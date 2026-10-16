import json
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest
from click import unstyle
from sqlalchemy import select
from typer.testing import CliRunner

from backend.app.db import make_engine, make_session_factory
from backend.cli import main as cli_main
from backend.models.audit import AuditEvent
from backend.models.user import User
from backend.scripts.migrate_data import (
    MigrationPrecheckError,
    MigrationReport,
    TableMigrationResult,
)
from backend.scripts.seed_dev import seed_default_workspace
from backend.services.credential_hashing import CredentialHasher
from backend.services.session_service import (
    InvalidCredentialsError,
    InvalidSessionError,
    SessionService,
    SessionTokenCodec,
)
from backend.tests.utils.bootstrapped_sqlite import copy_migrated_sqlite
from backend.tests.utils.environment import child_env_without_dtxwiki_config


def test_help_lists_core_commands():
    result = CliRunner().invoke(cli_main.app, ["--help"])

    assert result.exit_code == 0
    for name in [
        "serve",
        "bootstrap",
        "info",
        "migrate-data",
        "mcp",
        "users",
    ]:
        assert name in result.stdout
    for removed_name in ["claude", "cursor", "codex", "gemini", "antigravity"]:
        assert removed_name not in result.stdout


def test_mcp_help_exposes_only_http_commands():
    result = CliRunner().invoke(cli_main.app, ["mcp", "--help"])

    assert result.exit_code == 0
    assert "register-http" in result.stdout
    assert "doctor" in result.stdout
    for removed_name in ["register-remote", "register ", "unregister"]:
        assert removed_name not in result.stdout


def test_package_exposes_no_stdio_mcp_entry_point():
    """The console script only.

    The modules it used to point at are covered by
    tests/static/test_retired_modules_stay_deleted.py, together with the seven
    other modules this revision retired.
    """

    repo_root = Path(__file__).resolve().parents[3]
    project = tomllib.loads((repo_root / "pyproject.toml").read_text(encoding="utf-8"))

    assert "dtxwiki-mcp" not in project["project"]["scripts"]


def test_version_does_not_start_server(monkeypatch):
    monkeypatch.setattr(
        cli_main,
        "run_server",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("server started")),
    )

    result = CliRunner().invoke(cli_main.app, ["--version"])

    assert result.exit_code == 0
    assert result.stdout.startswith("dtxwiki ")


def test_root_invocation_accepts_serve_options(monkeypatch):
    calls = []

    def fake_run_server(**kwargs):
        calls.append(kwargs)

    monkeypatch.setattr(cli_main, "run_server", fake_run_server)

    result = CliRunner().invoke(
        cli_main.app,
        ["--no-open", "--port", "19999", "--log-level", "warning"],
    )

    assert result.exit_code == 0
    assert calls == [
        {
            "host": "127.0.0.1",
            "port": 19999,
            "db": None,
            "open_browser": False,
            "log_level": "warning",
            "worker_mode": None,
        }
    ]


def test_root_invocation_uses_env_backed_default_port(monkeypatch):
    calls = []

    async def fake_runtime(app_instance, settings, **kwargs):
        calls.append({"app": app_instance, **kwargs})

    monkeypatch.setenv("DTXWIKI_BACKEND_PORT", "22080")
    monkeypatch.setattr(cli_main, "bootstrap_app", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(cli_main, "create_app", lambda settings: object())
    monkeypatch.setattr(cli_main, "serve_runtime", fake_runtime)

    result = CliRunner().invoke(cli_main.app, ["--no-open"])

    assert result.exit_code == 0
    assert len(calls) == 1
    assert calls[0]["host"] == "127.0.0.1"
    assert calls[0]["port"] == 22080
    assert calls[0]["log_level"] == "info"
    assert calls[0]["proxy_headers"] is False
    assert calls[0]["forwarded_allow_ips"] == "127.0.0.1"
    assert calls[0]["on_started"] is None
    assert callable(calls[0]["bootstrap"])


def test_cli_import_does_not_create_global_asgi_app_under_production_guard():
    repo_root = Path(__file__).resolve().parents[3]
    env = child_env_without_dtxwiki_config()
    env.update(
        {
            "PYTHONPATH": str(repo_root),
            "DTXWIKI_APP_ENV": "production",
            "DTXWIKI_AUTO_BOOTSTRAP": "true",
        }
    )

    result = subprocess.run(
        [sys.executable, "-c", "import backend.cli.main; print('ok')"],
        cwd=repo_root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"


def test_bootstrap_command_forces_maintenance_bootstrap(monkeypatch, tmp_path):
    captured = {}

    def fake_bootstrap(settings):
        captured["auto_bootstrap"] = settings.auto_bootstrap
        captured["database_url"] = settings.database_url

    database_url = f"sqlite:///{tmp_path / 'bootstrap.sqlite'}"
    monkeypatch.setenv("DTXWIKI_AUTO_BOOTSTRAP", "false")
    monkeypatch.setattr(cli_main, "bootstrap_app", fake_bootstrap)

    result = CliRunner().invoke(cli_main.app, ["bootstrap", "--db", database_url])

    assert result.exit_code == 0
    assert captured == {
        "auto_bootstrap": True,
        "database_url": database_url,
    }


def test_bootstrap_command_echoes_redacted_database_url(monkeypatch):
    monkeypatch.setattr(cli_main, "bootstrap_app", lambda settings: None)
    database_url = (
        "postgresql+pg8000://dtx:s3cret@db.internal:5432/dtx?sslpassword=keypass"
    )

    result = CliRunner().invoke(cli_main.app, ["bootstrap", "--db", database_url])

    assert result.exit_code == 0
    assert "s3cret" not in result.stdout
    assert "keypass" not in result.stdout
    assert "bootstrapped postgresql+pg8000://dtx:****@db.internal:5432/dtx" in (
        result.stdout
    )
    assert "sslpassword=****" in result.stdout


def test_info_json_output_contains_install_state(tmp_path, monkeypatch):
    monkeypatch.setenv("DTXWIKI_DATA_DIR", str(tmp_path / "dtxwiki"))

    result = CliRunner().invoke(cli_main.app, ["info", "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["data_dir"] == str(tmp_path / "dtxwiki")
    assert payload["database_url"].startswith("sqlite+pysqlite:///")
    assert "detected_mcp_clients" in payload
    assert "dtxwiki_mcp_path" not in payload


def test_migrate_data_command_outputs_json(monkeypatch):
    def fake_migrate_data(*args, **kwargs):
        assert args == ("sqlite:///source.sqlite", "sqlite:///target.sqlite")
        assert kwargs["dry_run"] is True
        assert kwargs["batch_size"] == 25
        assert kwargs["force"] is False
        kwargs["on_progress"](
            cli_main.ProgressEvent(
                table_name="workspace",
                table_index=1,
                table_count=1,
                source_rows=1,
                inserted_rows=0,
                skipped_rows=0,
                dry_run=True,
            )
        )
        return MigrationReport(
            tables={
                "workspace": TableMigrationResult(
                    table_name="workspace",
                    source_rows=1,
                    inserted_rows=0,
                    skipped_rows=0,
                    elapsed_seconds=0.01,
                )
            },
            dry_run=True,
            source_revision="head",
            target_revision="head",
        )

    monkeypatch.setattr(cli_main, "run_migrate_data", fake_migrate_data)

    result = CliRunner().invoke(
        cli_main.app,
        [
            "migrate-data",
            "--from",
            "sqlite:///source.sqlite",
            "--to",
            "sqlite:///target.sqlite",
            "--dry-run",
            "--batch-size",
            "25",
            "--log-format",
            "json",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["dry_run"] is True
    assert payload["source_rows"] == 1


def test_migrate_data_command_reports_precheck_failure(monkeypatch):
    def fail_migrate_data(*_args, **_kwargs):
        raise MigrationPrecheckError("target database already contains user data")

    monkeypatch.setattr(cli_main, "run_migrate_data", fail_migrate_data)

    result = CliRunner().invoke(
        cli_main.app,
        [
            "migrate-data",
            "--from",
            "sqlite:///source.sqlite",
            "--to",
            "sqlite:///target.sqlite",
            "--yes",
        ],
    )

    assert result.exit_code == 2
    assert "migration precheck failed" in result.stderr


def test_users_add_and_reset_password(tmp_path, monkeypatch):
    database_url = f"sqlite:///{tmp_path / 'users.db'}"
    runner = CliRunner()
    monkeypatch.setattr(
        cli_main.admin_commands,
        "cli_operator_actor_id",
        lambda: "cli:test-operator",
    )

    created = runner.invoke(
        cli_main.app,
        [
            "users",
            "add",
            "User@Example.COM",
            "--password",
            "correct-pass",
            "--db",
            database_url,
            "--json",
        ],
    )

    assert created.exit_code == 0
    payload = json.loads(created.stdout)
    assert payload["email"] == "user@example.com"
    assert "role" not in payload

    session_service = make_session_service(database_url)
    assert (
        session_service.login(
            email="user@example.com",
            password="correct-pass",
        ).user.email
        == "user@example.com"
    )
    existing_session = session_service.login(
        email="user@example.com",
        password="correct-pass",
    )

    reset = runner.invoke(
        cli_main.app,
        [
            "users",
            "reset-password",
            "user@example.com",
            "--password",
            "changed-pass",
            "--db",
            database_url,
            "--json",
        ],
    )

    assert reset.exit_code == 0
    assert "deprecated" in reset.stderr
    with pytest.raises(InvalidSessionError, match="session token has been revoked"):
        session_service.authenticate_session(existing_session.session_token)
    with pytest.raises(InvalidCredentialsError, match="email or password is invalid"):
        session_service.login(email="user@example.com", password="correct-pass")
    assert (
        session_service.login(
            email="user@example.com",
            password="changed-pass",
        ).user.email
        == "user@example.com"
    )

    session_factory = make_session_factory(make_engine(database_url))
    with session_factory() as session:
        user = session.scalar(select(User).where(User.email == "user@example.com"))
        audit = session.scalar(
            select(AuditEvent).where(AuditEvent.action == "auth.password.admin_reset")
        )
        assert user is not None
        assert user.session_generation == 1
        assert audit is not None
        assert audit.workspace_id is None
        assert audit.actor_kind == "system"
        assert audit.actor_id == "cli:test-operator"
        assert audit.target_kind == "user"
        assert audit.target_id == user.id
        assert audit.retention_class == "sensitive"
        assert audit.payload == {
            "requested_via": "cli",
            "execution_path": "dtxwiki users reset-password",
            "sessions_revoked": True,
            "api_tokens_revoked": False,
        }


def test_users_add_reports_duplicate_email(tmp_path):
    database_url = copy_migrated_sqlite(tmp_path / "users.db")
    runner = CliRunner()
    args = [
        "users",
        "add",
        "dup@example.com",
        "--password",
        "correct-pass",
        "--db",
        database_url,
    ]

    assert runner.invoke(cli_main.app, args).exit_code == 0
    duplicate = runner.invoke(cli_main.app, args)

    assert duplicate.exit_code == 2
    assert "user add failed" in duplicate.stderr


@pytest.mark.parametrize(
    "arguments",
    (
        ["workspace", "members", "list"],
        ["workspace", "members", "add", "member@example.com"],
        ["workspace", "members", "remove", "member@example.com"],
    ),
)
def test_workspace_member_commands_require_explicit_workspace(arguments):
    result = CliRunner().invoke(cli_main.app, arguments)

    assert result.exit_code == 2
    assert "--workspace" in unstyle(result.output)


def test_workspace_members_add_list_and_remove(tmp_path):
    database_url = copy_migrated_sqlite(tmp_path / "members.db")
    runner = CliRunner()
    created = runner.invoke(
        cli_main.app,
        [
            "users",
            "add",
            "member@example.com",
            "--password",
            "correct-pass",
            "--db",
            database_url,
        ],
    )
    assert created.exit_code == 0

    session_factory = make_session_factory(make_engine(database_url))
    with session_factory.begin() as session:
        seed_default_workspace(session)

    added = runner.invoke(
        cli_main.app,
        [
            "workspace",
            "members",
            "add",
            "member@example.com",
            "--workspace",
            "default",
            "--role",
            "viewer",
            "--db",
            database_url,
            "--json",
        ],
    )
    assert added.exit_code == 0

    listed = runner.invoke(
        cli_main.app,
        [
            "workspace",
            "members",
            "list",
            "--workspace",
            "default",
            "--db",
            database_url,
            "--json",
        ],
    )
    assert listed.exit_code == 0
    assert json.loads(listed.stdout)[0]["role"] == "viewer"

    promoted = runner.invoke(
        cli_main.app,
        [
            "workspace",
            "members",
            "add",
            "member@example.com",
            "--workspace",
            "default",
            "--role",
            "editor",
            "--db",
            database_url,
            "--json",
        ],
    )
    assert promoted.exit_code == 0
    assert json.loads(promoted.stdout)["role"] == "editor"

    removed = runner.invoke(
        cli_main.app,
        [
            "workspace",
            "members",
            "remove",
            "member@example.com",
            "--workspace",
            "default",
            "--db",
            database_url,
            "--json",
        ],
    )
    assert removed.exit_code == 0
    assert json.loads(removed.stdout)["removed"] is True


def make_session_service(database_url: str) -> SessionService:
    return SessionService(
        session_factory=make_session_factory(make_engine(database_url)),
        credential_hasher=CredentialHasher(),
        token_codec=SessionTokenCodec(secret="test-cli-secret"),
    )

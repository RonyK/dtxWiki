"""Maintenance commands find the container's generated session secret.

`docker compose exec app dtxwiki users add ...` runs outside the entrypoint
that generated the secret, with DTXWIKI_SESSION_SECRET empty. Reading
DTXWIKI_SESSION_SECRET_FILE is what lets it share the app's secret instead
of refusing to start in team mode (readiness finding B-7).
"""

from __future__ import annotations

from argon2 import PasswordHasher, Type
from typer.testing import CliRunner

from backend.cli import main as cli_main
from backend.cli.commands import admin as cli_admin
from backend.services.credential_hashing import CredentialHasher
from backend.tests.utils.environment import clear_ambient_dtxwiki_env


def _fast_hasher() -> CredentialHasher:
    return CredentialHasher(
        PasswordHasher(
            time_cost=1,
            memory_cost=1024,
            parallelism=1,
            hash_len=16,
            salt_len=16,
            type=Type.ID,
        )
    )


def _container_env(monkeypatch, tmp_path) -> None:
    clear_ambient_dtxwiki_env(monkeypatch)
    monkeypatch.setenv("DTXWIKI_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("DTXWIKI_APP_MODE", "team")
    # docker-compose.yml passes the variable through even when unset.
    monkeypatch.setenv("DTXWIKI_SESSION_SECRET", "")
    monkeypatch.setattr(cli_admin, "CredentialHasher", _fast_hasher)


def _users_add(tmp_path):
    return CliRunner().invoke(
        cli_main.app,
        [
            "users",
            "add",
            "admin@example.com",
            "--password",
            "correct-pass",
            "--db",
            f"sqlite:///{tmp_path / 'team.sqlite'}",
        ],
    )


def test_users_add_reads_the_session_secret_file_in_team_mode(tmp_path, monkeypatch):
    _container_env(monkeypatch, tmp_path)
    secret_file = tmp_path / "session_secret"
    secret_file.write_text("container-generated-" + "c" * 60 + "\n", encoding="utf-8")
    monkeypatch.setenv("DTXWIKI_SESSION_SECRET_FILE", str(secret_file))

    result = _users_add(tmp_path)

    assert result.exit_code == 0, result.output
    assert "created user admin@example.com" in result.output


def test_users_add_without_any_secret_still_refuses_team_mode(tmp_path, monkeypatch):
    _container_env(monkeypatch, tmp_path)

    result = _users_add(tmp_path)

    assert result.exit_code != 0
    assert "DTXWIKI_SESSION_SECRET" in str(result.exception)

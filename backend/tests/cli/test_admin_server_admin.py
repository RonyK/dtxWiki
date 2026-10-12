"""CLI tests for `dtxwiki admin grant|revoke|list` (instance server-admins)."""

import json

from argon2 import PasswordHasher, Type
from typer.testing import CliRunner

from backend.cli import main as cli_main
from backend.cli.commands import admin as cli_admin
from backend.services.credential_hashing import CredentialHasher
from backend.tests.utils.bootstrapped_sqlite import copy_migrated_sqlite


def fast_hasher() -> CredentialHasher:
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


def _db(tmp_path) -> str:
    return copy_migrated_sqlite(tmp_path / "admin.sqlite")


def _add_user(runner, db, monkeypatch, email) -> None:
    monkeypatch.setattr(cli_admin, "CredentialHasher", fast_hasher)
    result = runner.invoke(
        cli_main.app,
        ["users", "add", email, "--password", "correct-pass", "--db", db],
    )
    assert result.exit_code == 0, result.output


def test_grant_list_revoke_roundtrip(tmp_path, monkeypatch):
    db = _db(tmp_path)
    runner = CliRunner()
    _add_user(runner, db, monkeypatch, "admin@example.com")

    granted = runner.invoke(
        cli_main.app, ["admin", "grant", "--email", "admin@example.com", "--db", db]
    )
    assert granted.exit_code == 0, granted.output
    assert "granted server-admin to admin@example.com" in granted.output

    listed = runner.invoke(cli_main.app, ["admin", "list", "--db", db, "--json"])
    assert listed.exit_code == 0, listed.output
    rows = json.loads(listed.output)
    assert [row["email"] for row in rows] == ["admin@example.com"]
    assert rows[0]["granted_by"] == "cli"

    revoked = runner.invoke(
        cli_main.app, ["admin", "revoke", "--email", "admin@example.com", "--db", db]
    )
    assert revoked.exit_code == 0, revoked.output
    assert "revoked server-admin from admin@example.com" in revoked.output

    empty = runner.invoke(cli_main.app, ["admin", "list", "--db", db, "--json"])
    assert json.loads(empty.output) == []


def test_grant_unknown_email_fails(tmp_path, monkeypatch):
    db = _db(tmp_path)
    runner = CliRunner()
    _add_user(runner, db, monkeypatch, "real@example.com")
    result = runner.invoke(
        cli_main.app, ["admin", "grant", "--email", "ghost@example.com", "--db", db]
    )
    assert result.exit_code == 2
    assert "no user with email" in result.output


def test_revoke_non_admin_reports_noop(tmp_path, monkeypatch):
    db = _db(tmp_path)
    runner = CliRunner()
    _add_user(runner, db, monkeypatch, "plain@example.com")
    result = runner.invoke(
        cli_main.app, ["admin", "revoke", "--email", "plain@example.com", "--db", db]
    )
    assert result.exit_code == 0, result.output
    assert "was not a server-admin" in result.output

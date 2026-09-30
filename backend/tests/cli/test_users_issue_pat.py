import json
from datetime import UTC, datetime, timedelta

from argon2 import PasswordHasher, Type
from typer.testing import CliRunner

from backend.app.db import make_session_factory_from_settings
from backend.app.settings import Settings
from backend.cli import main as cli_main
from backend.cli.commands import admin as cli_admin
from backend.models.api_token import ApiToken
from backend.models.user import User
from backend.models.workspace import Workspace
from backend.services.credential_hashing import CredentialHasher


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


def _database_url(tmp_path):
    return f"sqlite:///{tmp_path / 'issue-pat.sqlite'}"


def _create_user(
    runner: CliRunner,
    database_url: str,
    monkeypatch,
    email="pat@example.com",
) -> str:
    monkeypatch.setattr(cli_admin, "CredentialHasher", fast_hasher)
    result = runner.invoke(
        cli_main.app,
        [
            "users",
            "add",
            email,
            "--password",
            "correct-pass",
            "--db",
            database_url,
        ],
    )
    assert result.exit_code == 0, result.stderr
    session_factory = make_session_factory_from_settings(
        Settings(database_url=database_url)
    )
    with session_factory() as session:
        user = session.query(User).filter_by(email=email).one()
        workspace = session.get(Workspace, user.personal_workspace_id)
        assert workspace is not None
        return workspace.slug


def _issue_pat(
    runner: CliRunner,
    database_url: str,
    workspace_slug: str,
    *extra: str,
):
    return runner.invoke(
        cli_main.app,
        [
            "users",
            "issue-pat",
            "--user",
            "pat@example.com",
            "--workspace",
            workspace_slug,
            "--name",
            "remote-client",
            "--scope",
            "mcp:read,mcp:write",
            "--db",
            database_url,
            *extra,
        ],
    )


def test_issue_pat_outputs_plain_token_once(tmp_path, monkeypatch):
    database_url = _database_url(tmp_path)
    runner = CliRunner()
    workspace_slug = _create_user(runner, database_url, monkeypatch)

    result = _issue_pat(runner, database_url, workspace_slug)

    assert result.exit_code == 0, result.stderr
    assert result.stdout.count("dtxw_") == 1
    assert "only time it is shown" in result.stdout


def test_issue_pat_stores_hashed(tmp_path, monkeypatch):
    database_url = _database_url(tmp_path)
    runner = CliRunner()
    workspace_slug = _create_user(runner, database_url, monkeypatch)

    result = _issue_pat(runner, database_url, workspace_slug, "--json")

    assert result.exit_code == 0, result.stderr
    plain_token = json.loads(result.stdout)["plain_token"]
    session_factory = make_session_factory_from_settings(
        Settings(database_url=database_url)
    )
    with session_factory() as session:
        token = session.query(ApiToken).one()
        workspace = session.query(Workspace).filter_by(slug=workspace_slug).one()
        live_workspace_ids = {
            grant.workspace_id for grant in token.workspaces if grant.removed_at is None
        }
        assert live_workspace_ids == {workspace.id}
    assert plain_token not in token.token_lookup_hash
    assert plain_token not in token.hashed_token
    assert token.hashed_token.startswith("$argon2id$")
    assert fast_hasher().verify_api_token(plain_token, token.hashed_token)


def test_issue_pat_scope_validation_includes_agent(tmp_path, monkeypatch):
    database_url = _database_url(tmp_path)
    runner = CliRunner()
    workspace_slug = _create_user(runner, database_url, monkeypatch)

    result = runner.invoke(
        cli_main.app,
        [
            "users",
            "issue-pat",
            "--user",
            "pat@example.com",
            "--workspace",
            workspace_slug,
            "--name",
            "agent",
            "--scope",
            "mcp:agent",
            "--db",
            database_url,
            "--json",
        ],
    )

    assert result.exit_code == 0, result.stderr
    assert json.loads(result.stdout)["scopes"] == ["mcp:agent"]


def test_issue_pat_requires_explicit_workspace(tmp_path, monkeypatch):
    database_url = _database_url(tmp_path)
    runner = CliRunner()
    _create_user(runner, database_url, monkeypatch)

    result = runner.invoke(
        cli_main.app,
        [
            "users",
            "issue-pat",
            "--user",
            "pat@example.com",
            "--name",
            "missing-workspace",
            "--scope",
            "mcp:read",
            "--db",
            database_url,
        ],
    )

    assert result.exit_code == 2
    assert "--workspace" in result.output


def test_issue_pat_scope_validation_rejects_unknown(tmp_path, monkeypatch):
    database_url = _database_url(tmp_path)
    runner = CliRunner()
    workspace_slug = _create_user(runner, database_url, monkeypatch)

    result = _issue_pat(
        runner,
        database_url,
        workspace_slug,
        "--scope",
        "workspace-admin",
    )

    assert result.exit_code == 1
    assert "scope" in result.stderr


def test_issue_pat_expires_at_iso8601(tmp_path, monkeypatch):
    database_url = _database_url(tmp_path)
    runner = CliRunner()
    workspace_slug = _create_user(runner, database_url, monkeypatch)
    expires_at = (datetime.now(UTC) + timedelta(days=30)).replace(microsecond=0)

    result = _issue_pat(
        runner,
        database_url,
        workspace_slug,
        "--expires-at",
        expires_at.isoformat().replace("+00:00", "Z"),
        "--json",
    )

    assert result.exit_code == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["expires_at"].endswith("Z")


def test_issue_pat_user_not_found_exit_nonzero(tmp_path, monkeypatch):
    database_url = _database_url(tmp_path)
    monkeypatch.setattr(cli_admin, "CredentialHasher", fast_hasher)

    result = CliRunner().invoke(
        cli_main.app,
        [
            "users",
            "issue-pat",
            "--user",
            "missing@example.com",
            "--workspace",
            "missing-workspace",
            "--name",
            "missing",
            "--scope",
            "mcp:read",
            "--db",
            database_url,
        ],
    )

    assert result.exit_code == 1
    assert "user" in result.stderr
    assert "dtxw_" not in result.stdout


def test_issue_pat_json_output_shape(tmp_path, monkeypatch):
    database_url = _database_url(tmp_path)
    runner = CliRunner()
    workspace_slug = _create_user(runner, database_url, monkeypatch)

    result = _issue_pat(runner, database_url, workspace_slug, "--json")

    assert result.exit_code == 0, result.stderr
    payload = json.loads(result.stdout)
    assert set(payload) == {"plain_token", "token_id", "scopes", "expires_at", "name"}
    assert payload["plain_token"].startswith("dtxw_")
    assert payload["name"] == "remote-client"

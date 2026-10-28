"""CLI tests for `dtxwiki admin grant|revoke|list` (instance server-admins)."""

import json
from types import SimpleNamespace
from uuid import UUID

from argon2 import PasswordHasher, Type
from typer.testing import CliRunner

from backend.app import plugins
from backend.app.db import make_session_factory_from_settings
from backend.app.settings import Settings
from backend.cli import main as cli_main
from backend.cli.commands import admin as cli_admin
from backend.models.auth_identity import UserIdentity
from backend.models.user import User
from backend.services.credential_hashing import CredentialHasher
from backend.spi.plugins import DtxwikiPlugin
from backend.testing.fake_auth_method import FAKE_ISSUER, FakeAuthMethod
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
        ["users", "add", email, "--password", "correct-password", "--db", db],
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

    # SSO accounts have no native email; operator paths must use their exact UUID.
    factory = make_session_factory_from_settings(Settings(database_url=db))
    with factory.begin() as session:
        sso_user = User(email=None, password_hash=None, display_name="SSO Admin")
        session.add(sso_user)
        session.flush()
        identity = UserIdentity(
            user_id=sso_user.id,
            provider_slug="company",
            issuer=FAKE_ISSUER,
            subject="old-subject",
            linked_via="jit",
        )
        session.add(identity)
        session.flush()
        user_id, identity_id = str(sso_user.id), str(identity.id)
    granted = runner.invoke(
        cli_main.app, ["admin", "grant", "--user-id", user_id, "--db", db]
    )
    assert granted.exit_code == 0, granted.output
    listed = runner.invoke(cli_main.app, ["admin", "list", "--db", db, "--json"])
    assert [(row["user_id"], row["email"]) for row in json.loads(listed.output)] == [
        (user_id, None)
    ]
    for arguments in ([], ["--user-id", user_id, "--email", "admin@example.com"]):
        rejected = runner.invoke(
            cli_main.app, ["admin", "grant", *arguments, "--db", db]
        )
        assert rejected.exit_code == 2
        assert "exactly one" in rejected.output

    # The real command composes its configured provider and preserves the native
    # UUID; CAS failure afterwards must not perform a second recovery.
    method = FakeAuthMethod()
    plugin = DtxwikiPlugin(
        name="test_login",
        version="1.0",
        requires_core=">=0.1,<1",
        requires_spi=">=1.1,<2",
        auth_methods=lambda: [method],
    )
    monkeypatch.setattr(
        plugins.metadata,
        "entry_points",
        lambda **_: [SimpleNamespace(name="test_login", load=lambda: plugin)],
    )
    provider_file = tmp_path / "providers.toml"
    provider_file.write_text('[[providers]]\nslug="company"\nmethod="test_oidc"\n')
    monkeypatch.setenv("DTXWIKI_PLUGINS", "test_login")
    monkeypatch.setenv("DTXWIKI_AUTH_PROVIDERS_FILE", str(provider_file))
    monkeypatch.setenv("DTXWIKI_PUBLIC_BASE_URL", "https://wiki.example.test")
    command = [
        "users",
        "rebind-identity",
        "--user-id",
        user_id,
        "--identity-id",
        identity_id,
        "--expected-issuer",
        FAKE_ISSUER,
        "--expected-subject",
        "old-subject",
        "--provider",
        "company",
        "--issuer",
        FAKE_ISSUER,
        "--subject",
        "recovered-subject",
        "--reason",
        "Verified with company operator out of band",
        "--db",
        db,
    ]
    recovered = runner.invoke(cli_main.app, command)
    assert recovered.exit_code == 0, recovered.output
    assert "sessions and credentials revoked" in recovered.output
    stale = runner.invoke(cli_main.app, command)
    assert stale.exit_code == 2 and "identity_changed" in stale.output
    with factory() as session:
        recovered_user = session.get(User, UUID(user_id))
        assert recovered_user.email is None and recovered_user.password_hash is None
        assert recovered_user.session_generation == 1
        assert (
            session.get(UserIdentity, UUID(identity_id)).subject == "recovered-subject"
        )
    revoked = runner.invoke(
        cli_main.app, ["admin", "revoke", "--user-id", user_id, "--db", db]
    )
    assert revoked.exit_code == 0, revoked.output


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

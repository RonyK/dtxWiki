from datetime import UTC, datetime

from sqlalchemy import func, select
from typer.testing import CliRunner

from backend.app.db import make_engine, make_session_factory
from backend.cli import main as cli_main
from backend.models.audit import AuditEvent
from backend.models.job import Job
from backend.models.mail import EmailOutbox
from backend.models.password_reset import PasswordResetToken
from backend.models.user import User
from backend.services.mail_delivery import (
    EmailDeliveryService,
    MailDeliveryError,
    MailProviderError,
    SMTPMailProvider,
)


def test_issue_password_reset_auto_reveals_one_link_once(tmp_path, monkeypatch):
    database_url = f"sqlite:///{tmp_path / 'password-reset-cli.db'}"
    monkeypatch.setenv("DTXWIKI_PUBLIC_BASE_URL", "https://wiki.example.com")
    monkeypatch.setattr(cli_main.admin_commands, "cli_stdout_is_tty", lambda: True)
    monkeypatch.setattr(
        cli_main.admin_commands,
        "cli_operator_actor_id",
        lambda: "cli:test-operator",
    )
    runner = CliRunner()
    created = runner.invoke(
        cli_main.app,
        [
            "users",
            "add",
            "reset-cli@example.com",
            "--password",
            "original-password",
            "--db",
            database_url,
        ],
    )
    assert created.exit_code == 0, created.output

    issued = runner.invoke(
        cli_main.app,
        [
            "users",
            "issue-password-reset",
            "reset-cli@example.com",
            "--db",
            database_url,
        ],
    )

    assert issued.exit_code == 0, issued.output
    assert issued.stdout.count("dtxr_") == 1
    # M-2: the printed link carries the token in the fragment, which a browser
    # never sends -- so following it cannot write the token to an access log.
    assert "https://wiki.example.com/reset-password#token=dtxr_" in issued.stdout
    assert "?token=" not in issued.stdout
    assert "cannot be retrieved" in issued.stdout
    assert "api_and_mcp_tokens: keep active" in issued.stdout
    session_factory = make_session_factory(make_engine(database_url))
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(PasswordResetToken)) == 1


def test_issue_password_reset_email_never_falls_back_to_printed_link(
    tmp_path,
    monkeypatch,
):
    database_url = f"sqlite:///{tmp_path / 'password-reset-email.db'}"
    monkeypatch.setattr(cli_main.admin_commands, "cli_stdout_is_tty", lambda: True)
    result = CliRunner().invoke(
        cli_main.app,
        [
            "users",
            "issue-password-reset",
            "unknown@example.com",
            "--delivery",
            "email",
            "--db",
            database_url,
        ],
    )

    assert result.exit_code == 2
    assert "email delivery is not configured" in result.stderr
    assert "dtxr_" not in result.output
    assert "https://" not in result.output


def test_issue_password_reset_link_rejects_redirected_output_before_issuing(
    tmp_path,
    monkeypatch,
):
    database_url = f"sqlite:///{tmp_path / 'password-reset-redirect.db'}"
    monkeypatch.setattr(cli_main.admin_commands, "cli_stdout_is_tty", lambda: False)
    result = CliRunner().invoke(
        cli_main.app,
        [
            "users",
            "issue-password-reset",
            "unknown@example.com",
            "--delivery",
            "link",
            "--db",
            database_url,
        ],
    )

    assert result.exit_code == 2
    assert "interactive terminal" in result.stderr
    assert "dtxr_" not in result.output


def test_cli_provider_failure_commits_terminal_marker_and_audit(
    tmp_path,
    monkeypatch,
):
    database_url = f"sqlite:///{tmp_path / 'password-reset-provider-failure.db'}"
    monkeypatch.setenv("DTXWIKI_PUBLIC_BASE_URL", "https://wiki.example.com")
    monkeypatch.setenv("DTXWIKI_MAIL_PROVIDER", "smtp")
    monkeypatch.setenv("DTXWIKI_MAIL_FROM_ADDRESS", "wiki@example.com")
    monkeypatch.setenv("DTXWIKI_SMTP_HOST", "smtp.example.com")
    runner = CliRunner()
    created = runner.invoke(
        cli_main.app,
        [
            "users",
            "add",
            "provider-failure@example.com",
            "--password",
            "original-password",
            "--db",
            database_url,
        ],
    )
    assert created.exit_code == 0, created.output
    session_factory = make_session_factory(make_engine(database_url))
    with session_factory.begin() as session:
        user = session.scalar(
            select(User).where(User.email == "provider-failure@example.com")
        )
        assert user is not None
        user.email_verified_at = datetime.now(UTC)

    def fail_send(self, message):
        raise MailProviderError("mail provider delivery failed")

    monkeypatch.setattr(SMTPMailProvider, "send", fail_send)
    result = runner.invoke(
        cli_main.app,
        [
            "users",
            "issue-password-reset",
            "provider-failure@example.com",
            "--delivery",
            "email",
            "--db",
            database_url,
        ],
    )

    assert result.exit_code == 2
    assert "mail provider delivery failed" in result.stderr
    assert "dtxr_" not in result.output
    with session_factory() as session:
        outbox = session.scalar(select(EmailOutbox))
        events = list(
            session.scalars(
                select(AuditEvent).where(
                    AuditEvent.action == "auth.email.delivery.failed"
                )
            ).all()
        )
        assert outbox is not None and outbox.status == "failed"
        assert outbox.last_error == "mail_provider_failed"
        assert outbox.encrypted_token is None
        assert outbox.next_attempt_at is None
        # INVERTED by MAIL-02: this used to assert the matching email_delivery
        # Job also went terminal. No Job is created at all now; the CLI's
        # immediate delivery spends its whole (max_attempts=1) budget in one
        # call, and the orchestrator's settle phase writes the terminal marker
        # the deleted second transaction used to hand-roll.
        assert session.scalar(select(func.count()).select_from(Job)) == 0
        assert len(events) == 1
        assert events[0].payload == {
            "message_kind": "password_reset",
            "delivery_path": "cli_immediate",
            "error_code": "mail_provider_failed",
        }
        assert "provider-failure@example.com" not in str(events[0].payload)


def test_cli_render_failure_commits_terminal_marker_and_clears_envelope(
    tmp_path,
    monkeypatch,
):
    """MAIL-01 regression (F3): a RENDER failure on the CLI's immediate
    delivery must be observably identical to a provider failure.

    The guard above only ever exercised a provider failure. The render branch
    ran inside ``deliver``'s claim transaction, so its rollback discarded the
    terminal marker and the audit event the pre-MAIL-01 CLI wrote in a fresh
    transaction -- and, worst of all, left the recoverable encrypted reset
    envelope on the row where ``record_terminal_failure`` used to clear it.
    An expired, consumed or revoked-but-unfenced token, or an unset
    ``mail_from_address``, all reach this branch.
    """
    database_url = f"sqlite:///{tmp_path / 'password-reset-render-failure.db'}"
    monkeypatch.setenv("DTXWIKI_PUBLIC_BASE_URL", "https://wiki.example.com")
    monkeypatch.setenv("DTXWIKI_MAIL_PROVIDER", "smtp")
    monkeypatch.setenv("DTXWIKI_MAIL_FROM_ADDRESS", "wiki@example.com")
    monkeypatch.setenv("DTXWIKI_SMTP_HOST", "smtp.example.com")
    runner = CliRunner()
    created = runner.invoke(
        cli_main.app,
        [
            "users",
            "add",
            "render-failure@example.com",
            "--password",
            "original-password",
            "--db",
            database_url,
        ],
    )
    assert created.exit_code == 0, created.output
    session_factory = make_session_factory(make_engine(database_url))
    with session_factory.begin() as session:
        user = session.scalar(
            select(User).where(User.email == "render-failure@example.com")
        )
        assert user is not None
        user.email_verified_at = datetime.now(UTC)

    sends: list[object] = []

    def _never(self, message):  # pragma: no cover - must not be reached
        sends.append(message)

    def _unrenderable(self, session, outbox):
        # Exactly what _render raises for an expired/used/revoked token.
        raise MailDeliveryError("recovery token is no longer deliverable")

    monkeypatch.setattr(SMTPMailProvider, "send", _never)
    monkeypatch.setattr(EmailDeliveryService, "_render", _unrenderable)
    result = runner.invoke(
        cli_main.app,
        [
            "users",
            "issue-password-reset",
            "render-failure@example.com",
            "--delivery",
            "email",
            "--db",
            database_url,
        ],
    )

    assert result.exit_code == 2
    assert "password reset issue failed" in result.stderr
    assert "dtxr_" not in result.output
    assert sends == []
    with session_factory() as session:
        outbox = session.scalar(select(EmailOutbox))
        events = list(
            session.scalars(
                select(AuditEvent).where(
                    AuditEvent.action == "auth.email.delivery.failed"
                )
            ).all()
        )
        assert outbox is not None
        assert outbox.status == "failed"
        assert outbox.attempt_count == 1
        assert outbox.last_error == "mail_delivery_failed"
        assert outbox.next_attempt_at is None
        assert outbox.encrypted_token is None, (
            "an unrenderable reset must not keep recoverable reset material "
            "in the database"
        )
        assert len(events) == 1
        assert events[0].payload == {
            "message_kind": "password_reset",
            "delivery_path": "cli_immediate",
            "error_code": "mail_delivery_failed",
        }

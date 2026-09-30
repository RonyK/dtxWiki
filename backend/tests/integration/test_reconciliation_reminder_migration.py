from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect

from backend.app.bootstrap import resolve_alembic_ini, resolve_migrations_dir


def _config(database_url: str) -> Config:
    config = Config(str(resolve_alembic_ini()))
    config.set_main_option("script_location", str(resolve_migrations_dir()))
    config.set_main_option("sqlalchemy.url", database_url)
    return config


def _message_kind_check(database_url: str) -> str:
    engine = create_engine(database_url)
    try:
        constraints = inspect(engine).get_check_constraints("email_outbox")
    finally:
        engine.dispose()
    return next(
        str(constraint["sqltext"])
        for constraint in constraints
        if constraint["name"] == "email_outbox_message_kind_check"
    )


def _has_email_outbox(database_url: str) -> bool:
    engine = create_engine(database_url)
    try:
        return "email_outbox" in inspect(engine).get_table_names()
    finally:
        engine.dispose()


def test_epic1_round_trip_adds_final_notification_mail_kinds(
    tmp_path: Path,
):
    database_url = f"sqlite:///{tmp_path / 'reconciliation-reminder.db'}"
    config = _config(database_url)

    command.upgrade(config, "0001_initial_release")
    assert not _has_email_outbox(database_url)

    command.upgrade(config, "0002_epic1")
    upgraded_check = _message_kind_check(database_url)
    assert "job_failed" in upgraded_check
    assert "reconciliation_reminder" in upgraded_check

    command.downgrade(config, "0001_initial_release")
    assert not _has_email_outbox(database_url)

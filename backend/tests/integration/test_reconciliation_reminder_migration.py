from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect

from backend.tests.utils.migrated_sqlite_templates import shared_templates


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


@pytest.fixture(scope="session")
def sqlite_templates(tmp_path_factory):
    return shared_templates(tmp_path_factory)


def test_core_schema_has_final_notification_mail_kinds(
    tmp_path: Path, sqlite_templates
):
    database_url = sqlite_templates.clone("head", tmp_path / "reconciliation.db")

    upgraded_check = _message_kind_check(database_url)
    assert "job_failed" in upgraded_check
    assert "reconciliation_reminder" in upgraded_check

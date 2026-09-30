from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect
from sqlalchemy.exc import IntegrityError

from backend.app.bootstrap import resolve_alembic_ini, resolve_migrations_dir
from backend.models.context import ContextPage


def _config(database_url: str) -> Config:
    config = Config(str(resolve_alembic_ini()))
    config.set_main_option("script_location", str(resolve_migrations_dir()))
    config.set_main_option("sqlalchemy.url", database_url)
    return config


def test_workspace_access_schema_round_trip(tmp_path: Path):
    database_url = f"sqlite:///{tmp_path / 'workspace-access.db'}"
    config = _config(database_url)
    command.upgrade(config, "head")
    engine = create_engine(database_url)
    try:
        inspector = inspect(engine)
        assert "workspace_join_requests" in inspector.get_table_names()
        assert {
            column["name"]
            for column in inspector.get_columns("workspace_join_requests")
        } == {
            "id",
            "workspace_id",
            "requester_user_id",
            "status",
            "attempt_no",
            "requested_at",
            "updated_at",
            "decided_at",
            "decided_by_user_id",
            "decision_note",
            "closed_reason",
        }
        assert {
            constraint["name"]
            for constraint in inspector.get_check_constraints("workspace_join_requests")
        } == {
            "workspace_join_request_attempt_check",
            "workspace_join_request_status_check",
        }
        assert {
            constraint["name"]
            for constraint in inspector.get_unique_constraints(
                "workspace_join_requests"
            )
        } == {"workspace_join_request_user_workspace_uq"}
        assert {
            index["name"] for index in inspector.get_indexes("workspace_join_requests")
        } == {
            "idx_workspace_join_request_queue",
            "idx_workspace_join_request_requester",
        }
        assert "workspace_visibility_check" in {
            constraint["name"]
            for constraint in inspector.get_check_constraints("workspace")
        }

        engine.dispose()
        command.downgrade(config, "0001_initial_release")
        engine = create_engine(database_url)
        downgraded = inspect(engine)
        assert "workspace_join_requests" not in downgraded.get_table_names()
        assert "workspace_visibility_check" not in {
            constraint["name"]
            for constraint in downgraded.get_check_constraints("workspace")
        }
    finally:
        engine.dispose()


def test_current_schema_rejects_public_flag_without_complete_grant(page_service):
    created = page_service.write_page(
        workspace_slug="default",
        slug="legacy-public-flag",
        title="Legacy public flag",
        body_md="# Legacy public flag\n",
        actor_kind="human",
        actor_id="00000000-0000-0000-0000-000000000010",
        actor_provenance="browser_session",
    )
    with pytest.raises(IntegrityError), page_service.session_factory.begin() as session:
        page = session.get(ContextPage, created.id)
        assert page is not None
        page.is_public = True
        session.flush()

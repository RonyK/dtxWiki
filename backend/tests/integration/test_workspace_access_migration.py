from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect
from sqlalchemy.exc import IntegrityError

from backend.models.context import ContextPage
from backend.tests.utils.migrated_sqlite_templates import shared_templates


@pytest.fixture(scope="session")
def sqlite_templates(tmp_path_factory):
    return shared_templates(tmp_path_factory)


def test_workspace_access_schema_at_head(tmp_path: Path, sqlite_templates):
    database_url = sqlite_templates.clone("head", tmp_path / "workspace-access.db")
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
            "ck_workspace_join_requests_decision_note_input_v1",
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

from uuid import UUID

from sqlalchemy.dialects import postgresql

from backend.services.agent_page_service import (
    agent_page_by_id_for_update_statement,
    agent_page_by_slug_for_update_statement,
)
from backend.services.ask_service import ask_session_for_turn_insert_statement
from backend.services.context_ingress_service import (
    context_ingress_proposal_for_update_statement,
    context_page_for_ingress_update_statement,
)


def _postgres_sql(statement) -> str:
    return str(statement.compile(dialect=postgresql.dialect()))


def test_agent_page_ordered_writes_lock_by_id_and_slug():
    page_id = UUID("00000000-0000-0000-0000-000000000001")
    workspace_id = UUID("00000000-0000-0000-0000-000000000002")

    assert "FOR UPDATE" in _postgres_sql(
        agent_page_by_id_for_update_statement(workspace_id, page_id)
    )
    assert "FOR UPDATE" in _postgres_sql(
        agent_page_by_slug_for_update_statement(workspace_id, "persona")
    )


def test_ask_turn_number_allocation_locks_parent_session():
    ask_session_id = UUID("00000000-0000-0000-0000-000000000003")

    assert "FOR UPDATE" in _postgres_sql(
        ask_session_for_turn_insert_statement(ask_session_id)
    )


def test_context_ingress_locks_proposal_and_target_page_in_workspace():
    workspace_id = UUID("00000000-0000-0000-0000-000000000004")
    proposal_id = UUID("00000000-0000-0000-0000-000000000005")
    page_id = UUID("00000000-0000-0000-0000-000000000006")
    proposal_statement = context_ingress_proposal_for_update_statement(
        workspace_id,
        proposal_id,
    )
    page_statement = context_page_for_ingress_update_statement(
        workspace_id,
        page_id,
    )

    for statement in (proposal_statement, page_statement):
        sql = _postgres_sql(statement)
        assert "FOR UPDATE" in sql
        assert "workspace_id" in sql
        assert statement.get_execution_options()["populate_existing"] is True

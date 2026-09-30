from uuid import UUID

from sqlalchemy.dialects import postgresql

from backend.repositories.context_page_repo import (
    context_page_by_id_for_update_statement,
    context_page_by_id_for_version_insert_statement,
    context_page_by_slug_for_update_statement,
)


def test_version_insert_lock_statement_uses_for_update():
    page_id = UUID("00000000-0000-0000-0000-000000000001")
    statement = context_page_by_id_for_version_insert_statement(page_id)

    compiled = str(statement.compile(dialect=postgresql.dialect()))

    assert "FOR UPDATE" in compiled


def test_context_write_page_lookup_statements_use_for_update():
    page_id = UUID("00000000-0000-0000-0000-000000000001")
    workspace_id = UUID("00000000-0000-0000-0000-000000000002")

    by_id = str(
        context_page_by_id_for_update_statement(page_id).compile(
            dialect=postgresql.dialect()
        )
    )
    by_slug = str(
        context_page_by_slug_for_update_statement(workspace_id, "page-slug").compile(
            dialect=postgresql.dialect()
        )
    )

    assert "FOR UPDATE" in by_id
    assert "FOR UPDATE" in by_slug

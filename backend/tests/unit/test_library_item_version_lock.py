from uuid import UUID

from sqlalchemy.dialects import postgresql

from backend.services.library_service import (
    library_item_for_version_insert_statement,
)


def test_library_item_version_insert_lock_statement_uses_for_update():
    library_item_id = UUID("00000000-0000-0000-0000-000000000001")

    statement = library_item_for_version_insert_statement(library_item_id)
    compiled = str(statement.compile(dialect=postgresql.dialect()))

    assert "FOR UPDATE" in compiled
    assert statement.get_execution_options()["populate_existing"] is True

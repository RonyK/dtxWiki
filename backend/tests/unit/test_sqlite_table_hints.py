from sqlalchemy import select
from sqlalchemy.dialects import postgresql

from backend.app.db import make_engine
from backend.models.context import PageLink


def _hinted_page_link_select():
    return select(PageLink.source_page_id).with_hint(
        PageLink,
        "INDEXED BY idx_page_links_graph_head",
        dialect_name="sqlite",
    )


def test_make_engine_renders_sqlite_table_hints() -> None:
    engine = make_engine("sqlite+pysqlite:///:memory:")

    try:
        sql = str(_hinted_page_link_select().compile(dialect=engine.dialect))
    finally:
        engine.dispose()

    assert "FROM page_links INDEXED BY idx_page_links_graph_head" in sql


def test_sqlite_table_hints_do_not_leak_into_postgresql() -> None:
    sql = str(_hinted_page_link_select().compile(dialect=postgresql.dialect()))

    assert "INDEXED BY" not in sql

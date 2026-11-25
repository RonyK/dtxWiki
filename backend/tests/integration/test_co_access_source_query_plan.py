from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import event, inspect
from sqlalchemy.orm import Session

from backend.app.db import make_engine
from backend.models import Base
from backend.services.indexing.edge_registry import co_read_source

pytestmark = pytest.mark.integration

INDEX = "ix_mcp_chat_tool_call_artifact_workspace_time"


def test_co_read_source_reads_the_window_through_its_index(tmp_path) -> None:
    """The trail tick reads the 30-day window, not the whole per-call history.

    Per-call reads are never pruned, so without an index on the window every
    tick scanned every read the workspace ever made. The source still sorts
    the window into read order (USE TEMP B-TREE): the cost follows the rows
    in the window, which the pairing has to visit anyway.
    """

    engine = make_engine(f"sqlite+pysqlite:///{(tmp_path / 'co-read.db').as_posix()}")
    Base.metadata.create_all(engine)
    captured: list[tuple[str, Any]] = []

    def capture(_conn, _cursor, statement, parameters, _context, _executemany):
        if "FROM mcp_chat_tool_call_artifact" in statement:
            captured.append((statement, parameters))

    try:
        indexes = {
            index["name"]: index["column_names"]
            for index in inspect(engine).get_indexes("mcp_chat_tool_call_artifact")
        }
        assert indexes[INDEX] == ["workspace_id", "occurred_at"]

        event.listen(engine, "before_cursor_execute", capture)
        with Session(engine) as session:
            assert list(
                co_read_source(
                    session,
                    uuid4(),
                    since=datetime.now(UTC) - timedelta(days=30),
                )
            ) == []
        event.remove(engine, "before_cursor_execute", capture)

        assert len(captured) == 1
        statement, parameters = captured[0]
        with engine.connect() as connection:
            plan = [
                row[3]
                for row in connection.exec_driver_sql(
                    f"EXPLAIN QUERY PLAN {statement}", parameters
                )
            ]
    finally:
        engine.dispose()

    assert plan[0] == (
        f"SEARCH mcp_chat_tool_call_artifact USING INDEX {INDEX} "
        "(workspace_id=? AND occurred_at>?)"
    ), plan
    assert not any(detail.startswith("SCAN ") for detail in plan), plan

import sqlite3
from datetime import UTC, datetime
from unittest.mock import Mock

import pytest
from sqlalchemy import func, select

from backend.app.db import make_engine, make_session_factory
from backend.models.base import Base
from backend.models.workspace import Workspace
from backend.services.db_read_snapshot import acquire_consistent_read_snapshot
from backend.services.db_write_intent import acquire_write_intent


def test_sqlite_write_intent_works_with_create_all_database_and_serializes_writers(
    tmp_path,
):
    database_path = tmp_path / "write-intent.sqlite"
    engine = make_engine(f"sqlite+pysqlite:///{database_path.as_posix()}")
    Base.metadata.create_all(engine)
    session_factory = make_session_factory(engine)
    try:
        sentinel = datetime(2000, 1, 1, tzinfo=UTC)
        with session_factory.begin() as session:
            workspace = Workspace(
                slug="write-intent",
                title="Write intent",
                name="Write intent",
                description=None,
                updated_at=sentinel,
            )
            session.add(workspace)
            session.flush()
            workspace_id = workspace.id

        with session_factory() as session:
            workspace = session.get(Workspace, workspace_id)
            acquire_write_intent(session)
            acquire_write_intent(session)
            session.refresh(workspace)

            assert workspace.slug == "write-intent"
            assert workspace.updated_at == sentinel

            with sqlite3.connect(database_path, timeout=0) as contender:
                with pytest.raises(sqlite3.OperationalError, match="locked"):
                    contender.execute("BEGIN IMMEDIATE")

        with sqlite3.connect(database_path, timeout=0) as contender:
            contender.execute("BEGIN IMMEDIATE")
            contender.rollback()
    finally:
        engine.dispose()


def test_non_sqlite_write_intent_is_noop():
    session = Mock()
    session.get_bind.return_value.dialect.name = "postgresql"

    acquire_write_intent(session)

    session.execute.assert_not_called()


def test_sqlite_consistent_read_snapshot_survives_concurrent_commit(tmp_path):
    database_path = tmp_path / "read-snapshot.sqlite"
    engine = make_engine(f"sqlite+pysqlite:///{database_path.as_posix()}")
    Base.metadata.create_all(engine)
    session_factory = make_session_factory(engine)
    try:
        with session_factory.begin() as session:
            session.add(
                Workspace(
                    slug="snapshot-before",
                    title="Snapshot before",
                    name="Snapshot before",
                    description=None,
                )
            )

        with session_factory() as reader:
            acquire_consistent_read_snapshot(reader)
            before = reader.scalar(select(func.count()).select_from(Workspace))
            with session_factory.begin() as writer:
                writer.add(
                    Workspace(
                        slug="snapshot-after",
                        title="Snapshot after",
                        name="Snapshot after",
                        description=None,
                    )
                )
            after = reader.scalar(select(func.count()).select_from(Workspace))

        with session_factory() as observer:
            committed = observer.scalar(select(func.count()).select_from(Workspace))

        assert before == after == 1
        assert committed == 2
    finally:
        engine.dispose()


def test_postgresql_consistent_read_snapshot_is_repeatable_and_read_only():
    session = Mock()
    session.get_bind.return_value.dialect.name = "postgresql"
    connection = session.connection.return_value

    acquire_consistent_read_snapshot(session)

    session.connection.assert_called_once_with(
        execution_options={"isolation_level": "REPEATABLE READ"}
    )
    connection.exec_driver_sql.assert_called_once_with("SET TRANSACTION READ ONLY")

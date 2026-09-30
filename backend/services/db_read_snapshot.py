"""Consistent read snapshots for multi-query service projections."""

from sqlalchemy import text
from sqlalchemy.orm import Session

_SQLITE_BEGIN_READ = text("BEGIN")


def acquire_consistent_read_snapshot(session: Session) -> None:
    """Start one database snapshot before a multi-query read begins.

    Python's sqlite3 legacy transaction mode does not begin a transaction for
    a SELECT.  An explicit ``BEGIN`` is therefore required so later count and
    page queries keep seeing the same WAL snapshot while another connection
    commits.  PostgreSQL gets the equivalent read-only, repeatable-read
    transaction before its first SELECT.

    Callers must invoke this on a fresh Session, before any database statement.
    The surrounding Session context remains responsible for rollback/close.
    """

    dialect_name = session.get_bind().dialect.name
    if dialect_name == "sqlite":
        session.execute(_SQLITE_BEGIN_READ)
        return
    if dialect_name == "postgresql":
        connection = session.connection(
            execution_options={"isolation_level": "REPEATABLE READ"}
        )
        connection.exec_driver_sql("SET TRANSACTION READ ONLY")

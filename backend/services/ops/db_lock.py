"""Shared SQLite writability probe.

Used before any operation that mutates the SQLite file (data-migration,
update/backup) to fail fast with a clear message when another writer (the
server, worker, or an MCP process) still holds the database.
"""

from __future__ import annotations

from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError, SQLAlchemyError

from backend.services.ops.migration_preflight import make_non_mutating_engine


class SqliteLockedError(RuntimeError):
    """Raised when a SQLite database cannot take an immediate write lock."""


def is_sqlite_url(database_url: str) -> bool:
    """True for an on-disk SQLite URL (in-memory and non-SQLite are excluded).

    Uses ``make_url`` rather than substring checks so an in-memory DB (``:memory:``
    or ``?mode=memory``) or a file path that merely contains ``sqlite`` is
    classified correctly.
    """

    try:
        url = make_url(database_url)
    except ArgumentError:
        return False
    if url.get_backend_name() != "sqlite":
        return False
    database = url.database
    if not database or database == ":memory:":
        return False
    if (url.query.get("mode") or "") == "memory":
        return False
    return True


def probe_sqlite_writable(database_url: str) -> None:
    """Raise :class:`SqliteLockedError` if a SQLite DB is locked by a writer.

    No-op for non-SQLite or in-memory URLs (those have no exclusive-file lock to
    contend for here).
    """

    if not is_sqlite_url(database_url):
        return
    # Never the application engine: its ``journal_mode=WAL`` connect hook would
    # rewrite the header of the very file this probe promises only to test for
    # contention. ``query_only`` is off because ``BEGIN IMMEDIATE`` cannot take
    # the RESERVED lock on a read-only connection; the lock is taken and rolled
    # back without writing a byte.
    engine = make_non_mutating_engine(database_url, query_only=False)
    try:
        with engine.connect() as conn:
            try:
                conn.exec_driver_sql("BEGIN IMMEDIATE")
                conn.rollback()
            except SQLAlchemyError as exc:
                raise SqliteLockedError(
                    "SQLite database is locked; stop the dtxWiki server, worker, "
                    "and MCP processes before proceeding"
                ) from exc
    finally:
        engine.dispose()

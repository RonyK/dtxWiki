"""Dialect-portable Workspace media barrier coordination."""

from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from backend.models.media import WorkspaceMediaWriteIntent

_SQLITE_RESERVE_WRITER = text("BEGIN IMMEDIATE")


def reserve_sqlite_writer(session: Session) -> None:
    """Take SQLite's single-writer lock before any decision state is read.

    pysqlite opens a transaction only at the first DML statement, so every
    ``SELECT`` before it runs in autocommit mode against whatever was last
    committed: a sweep could read "no owner" while a registration held the
    write lock with its reference uncommitted, wait for the lock at its own
    first UPDATE, and then commit a decision made on that stale read. Opening
    the transaction with ``BEGIN IMMEDIATE`` first makes every later read part
    of the serialized write. PostgreSQL keeps relying on the row locks of the
    universal media lock order; this is a no-op there, and a no-op inside an
    already open SQLite transaction (whose first DML already took the lock).
    """

    connection = session.connection()
    if connection.dialect.name != "sqlite":
        return
    if not connection.connection.driver_connection.in_transaction:
        session.execute(_SQLITE_RESERVE_WRITER)


def lock_workspace_media_intent(
    session: Session,
    workspace_id: UUID,
    *,
    allow_deleting: bool,
) -> WorkspaceMediaWriteIntent:
    """Acquire the first lock in the universal media lock order.

    PostgreSQL uses a shared row lock so ordinary writers do not serialize each
    other; the hard-delete path takes an exclusive lock. SQLite ignores the
    clause and has one writer anyway, so the lock is SQLite's write lock,
    reserved before the intent row (or anything after it) is read.
    """

    reserve_sqlite_writer(session)
    intent = session.scalar(
        select(WorkspaceMediaWriteIntent)
        .where(WorkspaceMediaWriteIntent.workspace_id == workspace_id)
        .with_for_update(read=True)
    )
    if intent is None:
        raise ValueError("workspace_media_intent_not_configured")
    if intent.state == "deleting" and not allow_deleting:
        raise ValueError("workspace_media_deleting")
    if intent.state not in {"open", "deleting"}:
        raise RuntimeError("invalid_workspace_media_intent_state")
    return intent

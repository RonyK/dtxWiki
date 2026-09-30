"""Database write-intent acquisition shared by serialized write workflows."""

from sqlalchemy import text
from sqlalchemy.orm import Session

_SQLITE_WRITE_INTENT = text(
    "UPDATE workspace SET slug = slug WHERE 1 = 0"
)
_SQLITE_WORKSPACE_PLANE_WRITE_INTENT = text(
    "UPDATE context_page SET slug = slug WHERE 1 = 0"
)


def _acquire(session: Session, statement) -> None:
    if session.get_bind().dialect.name != "sqlite":
        return
    session.execute(statement)


def acquire_write_intent(session: Session) -> None:
    """Acquire SQLite's writer lock before reading mutable decision state.

    PostgreSQL callers keep using their narrower ``SELECT ... FOR UPDATE`` row
    locks.  On SQLite, pysqlite recognizes the zero-row UPDATE as DML, emits
    ``BEGIN IMMEDIATE``, and therefore takes the database RESERVED lock without
    changing a domain row.  Repeating the statement in one transaction is safe.

    ``workspace`` is part of both the initial Alembic schema and
    ``Base.metadata.create_all`` test databases.

    It is also a CONTROL-plane table (backend/tenancy_manifest.py), so this
    variant only serializes writes issued on a control session.  A workspace
    session may be bound to a shard that has no ``workspace`` table at all, in
    which case the lock statement itself fails -- use
    ``acquire_workspace_write_intent`` there.
    """

    _acquire(session, _SQLITE_WRITE_INTENT)


def acquire_workspace_write_intent(session: Session) -> None:
    """The same RESERVED-lock trick, aimed at a workspace-plane table.

    A SQLite write lock is database-wide, so which table the zero-row UPDATE
    names has no effect on what it serializes -- it only has to name a table
    that EXISTS in the database being locked.  ``context_page`` is the
    workspace plane's oldest table (initial Alembic revision and
    ``Base.metadata.create_all`` alike), so it is present on every shard a
    workspace session can be bound to, while ``workspace`` is not.
    """

    _acquire(session, _SQLITE_WORKSPACE_PLANE_WRITE_INTENT)

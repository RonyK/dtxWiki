"""One stamp/schema verdict shared by every migration entry point.

Bootstrap, the Alembic environment, and the data-migration script each used to
read ``alembic_version`` by hand and reach their own conclusion. This module is
the single reader. It takes a database URL or an already-open connection — never
:class:`~backend.app.settings.Settings` — so a future split-plane deployment can
ask for one verdict per database instead of one verdict per process.
"""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Literal

import sqlalchemy as sa
from sqlalchemy.engine import Connection, Engine, make_url
from sqlalchemy.exc import ArgumentError
from sqlalchemy.pool import NullPool

from backend.services.ops.alembic_migration_graph import code_revisions
from backend.services.ops.db_revision_inventory import (
    DatabaseSnapshot,
    ProbeFailure,
    classify_revision_consistency,
)
from backend.services.ops.read_only_database import inspect_revision_snapshot

PREFLIGHT_REFUSAL_TOKEN = "migration_preflight_refused"

MigrationPreflightVerdict = Literal["fresh", "ready", "blocked"]

#: The database file does not exist yet; nothing was opened to find out.
DATABASE_ABSENT = "database_absent"
#: The database exists and provably holds no table and no view of its own.
PROVEN_EMPTY = "proven_empty"
#: Neither a stamp nor a schema marker, but the database already holds data.
UNVERSIONED_POPULATED = "unversioned_populated"
#: ``alembic_version`` holds something that is not a revision identifier.
INVALID_ALEMBIC_REVISION = "invalid_alembic_revision"
#: The probe itself could not complete (unreadable file, unsupported driver).
PROBE_ERROR = "probe_error"

#: Excluded from the emptiness predicate: Alembic's own bookkeeping table says
#: nothing about whether a database carries user data.
_ALEMBIC_VERSION_TABLE = "alembic_version"
_APP_METADATA_TABLE = "app_metadata"


@dataclass(frozen=True)
class MigrationPreflight:
    """Whether a database may be migrated, and the evidence behind that.

    ``fresh`` is a database that is absent or provably empty, ``ready`` is a
    stamp that provably describes the schema it labels, and ``blocked`` is every
    other pairing — a laundered stamp, a retired revision, an unstamped schema
    that already holds data, multiple heads, or a probe that could not complete.
    """

    verdict: MigrationPreflightVerdict
    consistency: str
    revision: str | None
    snapshot: DatabaseSnapshot | None = field(repr=False, default=None)
    user_objects: tuple[str, ...] = field(repr=False, default=())
    #: ``app_metadata.dtxwiki_version`` as read by the same non-mutating
    #: connection, or ``None`` when it is absent or unreadable. It is what lets
    #: a refusal tell "written by a newer dtxWiki" apart from damage.
    recorded_app_version: str | None = None

    @property
    def blocked(self) -> bool:
        return self.verdict == "blocked"

    def refusal(self, subject: str) -> str:
        return (
            f"{subject}: {PREFLIGHT_REFUSAL_TOKEN}; DB Alembic stamp/schema "
            f"evidence is {self.consistency}; this database cannot be safely "
            "identified. Back it up and rebuild it (see DEPLOYMENT.md "
            "Troubleshooting); never repair it with a blind alembic stamp."
        )


def make_non_mutating_engine(database_url: str, *, query_only: bool = True) -> Engine:
    """Open a database without the application engine's WAL configuration.

    ``backend.app.db.make_engine`` issues ``PRAGMA journal_mode=WAL`` on every
    connect, which rewrites the header of a SQLite file that was only meant to
    be read. Preflight — and the read side of a data migration — must leave the
    file they inspect byte-identical.

    ``query_only=False`` keeps the WAL configuration off but allows the
    connection to take a write lock. Only the SQLite lock probe needs it:
    ``BEGIN IMMEDIATE`` fails outright on a ``query_only`` connection, and it is
    the lock, not a write, that the probe is after.
    """

    engine = sa.create_engine(database_url, poolclass=NullPool, future=True)
    if query_only and engine.dialect.name == "sqlite":
        sa.event.listen(engine, "connect", _set_sqlite_query_only)
    return engine


def _set_sqlite_query_only(dbapi_connection, _connection_record) -> None:
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA query_only=ON")
    cursor.close()


def evaluate_migration_preflight(database_url: str) -> MigrationPreflight:
    """Classify one database addressed by URL, without mutating it."""

    if _sqlite_file_is_absent(database_url):
        # Connecting would create the file. A first install is fresh either
        # way, so there is nothing to learn that is worth writing to disk.
        return MigrationPreflight(
            verdict="fresh",
            consistency=DATABASE_ABSENT,
            revision=None,
        )
    try:
        engine = make_non_mutating_engine(database_url)
    except Exception:
        return _unreadable(PROBE_ERROR)
    try:
        with engine.connect() as connection:
            preflight = evaluate_connection_preflight(connection)
            if _APP_METADATA_TABLE in preflight.user_objects:
                preflight = replace(
                    preflight,
                    recorded_app_version=_recorded_app_version(connection),
                )
            return preflight
    except Exception:
        # Connecting is the only step left that this frame owns; everything the
        # probe itself can fail at is classified inside the connection path.
        return _unreadable(PROBE_ERROR)
    finally:
        engine.dispose()


def evaluate_connection_preflight(
    connection: Connection,
    *,
    known_revisions: Collection[str] | None = None,
) -> MigrationPreflight:
    """Classify the database behind an already-open connection.

    Alembic and bootstrap already hold a migration connection and cannot open
    the operator CLI's file-level read-only adapter, so this entry point reads
    the same catalog evidence through the connection they own. Alembic passes
    the revisions of the script directory it runs (``known_revisions``);
    everyone else is judged against the one this code ships.
    """

    try:
        user_objects = _user_objects(connection)
        snapshot = inspect_revision_snapshot(connection)
    except ProbeFailure as exc:
        return _unreadable(exc.code)
    except Exception:
        return _unreadable(PROBE_ERROR)
    return preflight_for_snapshot(
        snapshot,
        user_objects,
        known_revisions=(
            code_revisions() if known_revisions is None else known_revisions
        ),
    )


def preflight_for_snapshot(
    snapshot: DatabaseSnapshot,
    user_objects: tuple[str, ...],
    *,
    known_revisions: Collection[str] | None = None,
) -> MigrationPreflight:
    consistency = classify_revision_consistency(
        snapshot, known_revisions=known_revisions
    )
    if consistency == "consistent":
        verdict: MigrationPreflightVerdict = "ready"
    elif consistency == "unversioned":
        # No stamp and no schema marker. That is a fresh install only when the
        # database is provably empty. An unstamped database that already holds
        # tables is somebody else's data — or a dtxWiki database whose schema we
        # cannot recognise — and migrating it in place would write our schema on
        # top of it on every restart.
        if user_objects:
            consistency = UNVERSIONED_POPULATED
            verdict = "blocked"
        else:
            consistency = PROVEN_EMPTY
            verdict = "fresh"
    else:
        verdict = "blocked"
    revisions = snapshot.alembic_revisions
    return MigrationPreflight(
        verdict=verdict,
        consistency=consistency,
        revision=revisions[0] if len(revisions) == 1 else None,
        snapshot=snapshot,
        user_objects=user_objects,
    )


def _recorded_app_version(connection: Connection) -> str | None:
    """Best-effort read of the version that last bootstrapped this database.

    Only the URL entry point calls this, on its own connection: a failed read
    inside a migration connection would abort a PostgreSQL transaction that
    Alembic still owns.
    """

    try:
        value = connection.execute(
            sa.text("SELECT value FROM app_metadata WHERE key = :key"),
            {"key": "dtxwiki_version"},
        ).scalar()
    except Exception:
        return None
    return value if isinstance(value, str) and value else None


def _user_objects(connection: Connection) -> tuple[str, ...]:
    """Every table and view the database owns, minus Alembic's bookkeeping.

    Scoped to the default search path by construction, so a PostgreSQL schema
    shared with another application reads as populated and is refused rather
    than silently migrated.
    """

    inspector = sa.inspect(connection)
    names = [
        name
        for name in inspector.get_table_names()
        if name != _ALEMBIC_VERSION_TABLE
    ]
    names.extend(inspector.get_view_names())
    return tuple(sorted(names))


def _sqlite_file_is_absent(database_url: str) -> bool:
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
    return not Path(database).exists()


def _unreadable(consistency: str) -> MigrationPreflight:
    return MigrationPreflight(
        verdict="blocked",
        consistency=consistency,
        revision=None,
        snapshot=None,
    )

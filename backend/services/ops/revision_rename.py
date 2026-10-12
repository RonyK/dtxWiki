"""Explicit, schema-checked conversion of the two pre-rename revision IDs.

This changes only Alembic bookkeeping. It never replays a migration, creates
an empty database, or accepts the retired intermediate migration graph.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, replace
from functools import lru_cache
from importlib import import_module
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy.engine import Connection

from backend.services.ops.db_revision_inventory import (
    RELEASED_BASE_REVISION,
    RELEASED_EPIC1_REVISION,
    classify_revision_consistency,
)
from backend.services.ops.migration_preflight import (
    evaluate_migration_preflight,
    make_non_mutating_engine,
)
from backend.services.ops.read_only_database import inspect_revision_snapshot

LEGACY_REVISION_ALIASES = {
    "0001_initial_release": RELEASED_BASE_REVISION,
    "0002_epic1": RELEASED_EPIC1_REVISION,
}


class RevisionRenameError(RuntimeError):
    """The database cannot be identified as a supported rename source."""


@dataclass(frozen=True)
class RevisionRenameResult:
    old_revision: str
    new_revision: str
    changed: bool


@lru_cache(maxsize=1)
def _initial_columns() -> dict[str, frozenset[str]]:
    """Read required columns from the frozen, literal baseline declarations.

    Absence of later schema markers alone cannot identify the initial schema:
    an incomplete initial DB also carries none of those markers.
    """

    initial = import_module(
        f"backend.migrations.versions.v0_0.{RELEASED_BASE_REVISION}_initial_schema"
    )
    declarations = ast.parse(Path(initial.__file__).read_text(encoding="utf-8"))
    tables = {}
    for call in ast.walk(declarations):
        if (
            isinstance(call, ast.Call)
            and isinstance(call.func, ast.Attribute)
            and isinstance(call.func.value, ast.Name)
            and call.func.value.id == "op"
            and call.func.attr == "create_table"
        ):
            tables[ast.literal_eval(call.args[0])] = frozenset(
                ast.literal_eval(column.args[0])
                for column in call.args[1:]
                if isinstance(column, ast.Call)
                and isinstance(column.func, ast.Attribute)
                and column.func.attr == "Column"
            )
    if set(tables) != set(initial.TABLE_NAMES):
        raise RevisionRenameError("revision_rename_refused: invalid baseline contract")
    return tables


def _convert(connection: Connection, *, apply: bool) -> RevisionRenameResult:
    snapshot = inspect_revision_snapshot(connection)
    if len(snapshot.alembic_revisions) != 1:
        raise RevisionRenameError("revision_rename_refused: expected one revision")
    old = snapshot.alembic_revisions[0]
    new = LEGACY_REVISION_ALIASES.get(old, old)
    if new not in {RELEASED_BASE_REVISION, RELEASED_EPIC1_REVISION}:
        raise RevisionRenameError("revision_rename_refused: unsupported revision")
    canonical = replace(snapshot, alembic_revisions=(new,))
    consistency = classify_revision_consistency(canonical)
    if consistency != "consistent":
        raise RevisionRenameError(f"revision_rename_refused: {consistency}")
    inspector = sa.inspect(connection)
    initial_columns = _initial_columns()
    if not initial_columns.keys() <= set(inspector.get_table_names()):
        raise RevisionRenameError("revision_rename_refused: partial_schema")
    if new == RELEASED_BASE_REVISION:
        for table, required in initial_columns.items():
            actual = {column["name"] for column in inspector.get_columns(table)}
            if not required <= actual:
                raise RevisionRenameError("revision_rename_refused: partial_schema")
    changed = apply and old != new
    if changed:
        updated = connection.execute(
            sa.text(
                "UPDATE alembic_version SET version_num = :new WHERE version_num = :old"
            ),
            {"old": old, "new": new},
        )
        if updated.rowcount != 1:
            raise RevisionRenameError("revision_rename_refused: concurrent change")
        after = inspect_revision_snapshot(connection)
        if after.alembic_revisions != (new,) or (
            classify_revision_consistency(after) != "consistent"
        ):
            raise RevisionRenameError("revision_rename_refused: verification failed")
    return RevisionRenameResult(old, new, changed)


def rename_database_revisions(
    database_url: str, *, apply: bool = False
) -> RevisionRenameResult:
    """Preview by default; apply under a transaction and database write lock.

    Operators must stop writers and back up the database before applying.
    The application startup path deliberately never invokes this conversion.
    """

    preflight = evaluate_migration_preflight(database_url)
    if preflight.verdict == "fresh":
        raise RevisionRenameError("revision_rename_refused: no existing revision")
    engine = make_non_mutating_engine(database_url, query_only=not apply)
    try:
        if apply and engine.dialect.name == "sqlite":

            @sa.event.listens_for(engine, "connect")
            def _disable_driver_begin(dbapi_connection, _record) -> None:
                dbapi_connection.isolation_level = None

            @sa.event.listens_for(engine, "begin")
            def _lock_sqlite(connection: Connection) -> None:
                connection.exec_driver_sql("BEGIN IMMEDIATE")

        with engine.begin() as connection:
            if apply and engine.dialect.name == "postgresql":
                connection.exec_driver_sql(
                    "LOCK TABLE alembic_version IN EXCLUSIVE MODE"
                )
            return _convert(connection, apply=apply)
    finally:
        engine.dispose()

"""Copy dtxWiki ORM data between SQLAlchemy backends.

Both source and target must already have the same logical schema applied.
The migration copies ORM-managed tables only; Alembic metadata is validated
before copying and is not copied as user data.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from time import monotonic
from typing import Any

from sqlalchemy import Engine, MetaData, Table, func, select
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from backend.app.db import make_engine
from backend.models import Base
from backend.services.ops import runtime_lock
from backend.services.ops.db_lock import SqliteLockedError, probe_sqlite_writable
from backend.services.ops.migration_preflight import (
    MigrationPreflight,
    evaluate_migration_preflight,
    make_non_mutating_engine,
)

DeferredUpdate = tuple[Table, Any, dict[str, Any]]


@dataclass(frozen=True)
class ProgressEvent:
    table_name: str
    table_index: int
    table_count: int
    source_rows: int
    inserted_rows: int
    skipped_rows: int
    dry_run: bool


@dataclass(frozen=True)
class TableMigrationResult:
    table_name: str
    source_rows: int
    inserted_rows: int
    skipped_rows: int
    elapsed_seconds: float


@dataclass(frozen=True)
class MigrationReport:
    tables: dict[str, TableMigrationResult] = field(default_factory=dict)
    dry_run: bool = False
    source_revision: str | None = None
    target_revision: str | None = None

    @property
    def source_rows(self) -> int:
        return sum(table.source_rows for table in self.tables.values())

    @property
    def inserted_rows(self) -> int:
        return sum(table.inserted_rows for table in self.tables.values())

    @property
    def skipped_rows(self) -> int:
        return sum(table.skipped_rows for table in self.tables.values())

    def values(self):
        """Compatibility shim for older tests that treated the report as a dict."""
        return (table.source_rows for table in self.tables.values())

    def as_dict(self) -> dict[str, Any]:
        return {
            "dry_run": self.dry_run,
            "source_revision": self.source_revision,
            "target_revision": self.target_revision,
            "source_rows": self.source_rows,
            "inserted_rows": self.inserted_rows,
            "skipped_rows": self.skipped_rows,
            "tables": {name: asdict(result) for name, result in self.tables.items()},
        }


class MigrationPrecheckError(RuntimeError):
    """Raised when migration safety checks reject a copy operation."""


def migrate_data(
    source_url: str,
    target_url: str,
    *,
    dry_run: bool = False,
    batch_size: int = 500,
    on_progress=None,
    force: bool = False,
    data_dir: Path | None = None,
) -> MigrationReport:
    """Copy every table in ``Base.metadata`` from source to target.

    Tables are ordered so that NOT NULL foreign keys are always satisfied.
    Nullable foreign keys (including self-FKs) are deferred: their columns
    are NULLed on the initial INSERT and UPDATEd in a second pass once all
    rows exist.

    The dtxWiki runtime lock in ``data_dir`` (default: the configured data
    directory) is held exclusively for the whole copy: a server or worker
    still running against the source would keep writing rows the copy has
    already passed.

    Returns a ``MigrationReport`` with per-table source, inserted, and skipped
    row counts.
    """
    if batch_size < 1:
        raise ValueError("batch_size must be >= 1")
    if source_url == target_url:
        raise MigrationPrecheckError("source_url and target_url must differ")

    try:
        held = runtime_lock.acquire_exclusive(data_dir)
    except runtime_lock.RuntimeLockHeldError as exc:
        raise MigrationPrecheckError(str(exc)) from exc
    except OSError as exc:
        raise MigrationPrecheckError(
            f"could not take the dtxWiki runtime lock: {exc}"
        ) from exc
    with held:
        return _migrate_data_locked(
            source_url,
            target_url,
            dry_run=dry_run,
            batch_size=batch_size,
            on_progress=on_progress,
            force=force,
        )


def _migrate_data_locked(
    source_url: str,
    target_url: str,
    *,
    dry_run: bool,
    batch_size: int,
    on_progress,
    force: bool,
) -> MigrationReport:
    # The stamp/schema verdict is taken before any engine is opened: a refused
    # database must not even be WAL-converted by the application engine.
    source_revision, target_revision = verify_alembic_revision(source_url, target_url)

    source = make_non_mutating_engine(source_url)
    target = make_engine(target_url)
    try:
        verify_target_empty(target, force=force)
        verify_source_writable_lock(source_url)
        ordered, deferred_cols = _resolve_order(Base.metadata)
        table_results: dict[str, TableMigrationResult] = {}
        pending: list[DeferredUpdate] = []
        for index, table in enumerate(ordered, start=1):
            result, deferred = _copy_table(
                source,
                target,
                table,
                deferred_cols.get(table.name, frozenset()),
                dry_run=dry_run,
                batch_size=batch_size,
            )
            table_results[table.name] = result
            pending.extend(deferred)
            if on_progress is not None:
                on_progress(
                    ProgressEvent(
                        table_name=table.name,
                        table_index=index,
                        table_count=len(ordered),
                        source_rows=result.source_rows,
                        inserted_rows=result.inserted_rows,
                        skipped_rows=result.skipped_rows,
                        dry_run=dry_run,
                    )
                )
        if not dry_run:
            _apply_deferred_updates(target, pending)
        return MigrationReport(
            tables=table_results,
            dry_run=dry_run,
            source_revision=source_revision,
            target_revision=target_revision,
        )
    finally:
        source.dispose()
        target.dispose()


def verify_alembic_revision(source_url: str, target_url: str) -> tuple[str, str]:
    source = _read_alembic_revision(source_url, "source")
    target = _read_alembic_revision(target_url, "target")
    if source.revision != target.revision:
        raise MigrationPrecheckError(
            "source and target alembic revisions differ: "
            f"{source.revision} != {target.revision}"
        )
    for label, preflight in (("source", source), ("target", target)):
        if preflight.verdict != "ready":
            raise MigrationPrecheckError(
                f"{label} database stamp/schema evidence is "
                f"{preflight.consistency}; refusing to copy a database whose "
                "schema identity cannot be proven"
            )
    assert source.revision is not None
    assert target.revision is not None
    return source.revision, target.revision


def verify_target_empty(target: Engine, *, force: bool) -> None:
    non_empty: list[str] = []
    with target.connect() as conn:
        for table in Base.metadata.tables.values():
            count = conn.execute(select(func.count()).select_from(table)).scalar() or 0
            if count:
                non_empty.append(table.name)
    if non_empty and not force:
        preview = ", ".join(non_empty[:5])
        if len(non_empty) > 5:
            preview = f"{preview}, ..."
        raise MigrationPrecheckError(
            "target database already contains user data; "
            f"pass --force to resume idempotently ({preview})"
        )


def verify_source_writable_lock(source_url: str) -> None:
    try:
        probe_sqlite_writable(source_url)
    except SqliteLockedError as exc:
        raise MigrationPrecheckError(
            "source SQLite database is locked; stop backend, worker, "
            "and MCP processes before migrating"
        ) from exc


def _read_alembic_revision(database_url: str, label: str) -> MigrationPreflight:
    """Read one end's stamp through the shared preflight, never mutating it."""

    preflight = evaluate_migration_preflight(database_url)
    snapshot = preflight.snapshot
    if snapshot is None:
        raise MigrationPrecheckError(
            f"{label} database has an unreadable or invalid alembic revision; "
            "run alembic upgrade head before migrating"
        )
    if not snapshot.alembic_table_present:
        raise MigrationPrecheckError(
            f"{label} database has no alembic_version table; "
            "run alembic upgrade head before migrating"
        )
    if not snapshot.alembic_revisions:
        raise MigrationPrecheckError(
            f"{label} database has no alembic revision row; "
            "run alembic upgrade head before migrating"
        )
    if len(snapshot.alembic_revisions) != 1:
        raise MigrationPrecheckError(
            f"{label} database has multiple alembic revision rows"
        )
    return preflight


def _resolve_order(
    metadata: MetaData,
) -> tuple[list[Table], dict[str, frozenset[str]]]:
    """Order tables by NOT NULL FK dependencies; collect deferred columns.

    Under the default ``MATCH SIMPLE`` semantics, a composite foreign key does
    not reference another row whenever *any* constrained column is NULL. Such
    FKs are deferred by NULLing only their nullable local columns on initial
    INSERT, then restoring those values after all rows exist. ``MATCH FULL``
    and other non-SIMPLE constraints are deferred only when every local column
    is nullable, because a partially NULL key is not an absent reference.
    """
    tables: list[Table] = list(metadata.tables.values())
    by_name: dict[str, Table] = {t.name: t for t in tables}

    deferred: dict[str, set[str]] = {}
    deps: dict[str, set[str]] = {t.name: set() for t in tables}

    for table in tables:
        for fkc in table.foreign_key_constraints:
            cols = list(fkc.columns)
            target_name = fkc.referred_table.name
            nullable_cols = [column for column in cols if column.nullable]
            match = (fkc.match or "SIMPLE").upper()
            can_defer = bool(nullable_cols) and (
                match == "SIMPLE" or len(nullable_cols) == len(cols)
            )
            if can_defer:
                deferred.setdefault(table.name, set()).update(
                    column.name for column in nullable_cols
                )
            elif target_name == table.name:
                raise RuntimeError(
                    f"{table.name} has a NOT NULL self-referential FK; "
                    "this case is not supported."
                )
            else:
                deps[table.name].add(target_name)

    ordered: list[Table] = []
    remaining = set(deps.keys())
    while remaining:
        ready = sorted(name for name in remaining if not deps[name])
        if not ready:
            raise RuntimeError(
                f"Cyclic NOT NULL foreign keys among: {sorted(remaining)}"
            )
        for name in ready:
            ordered.append(by_name[name])
            remaining.discard(name)
        for name in deps:
            deps[name].difference_update(ready)

    return ordered, {name: frozenset(cols) for name, cols in deferred.items()}


def _copy_table(
    source: Engine,
    target: Engine,
    table: Table,
    deferred_cols: frozenset[str],
    *,
    dry_run: bool,
    batch_size: int,
) -> tuple[TableMigrationResult, list[DeferredUpdate]]:
    started_at = monotonic()
    pending: list[DeferredUpdate] = []
    source_rows = 0
    inserted_rows = 0
    # Stream the source in batch_size chunks rather than materializing the
    # whole table, so --batch-size bounds memory on the read side too
    # (server-side cursor where the driver supports it; bounded fetchmany
    # everywhere).
    with source.connect() as src_conn:
        result = src_conn.execution_options(stream_results=True).execute(select(table))
        mappings = result.mappings()
        if dry_run:
            batch = mappings.fetchmany(batch_size)
            while batch:
                source_rows += len(batch)
                batch = mappings.fetchmany(batch_size)
        else:
            batch = mappings.fetchmany(batch_size)
            if batch:
                statement = _insert_ignore_statement(target, table)
                with target.begin() as dst_conn:
                    while batch:
                        rows = [dict(row) for row in batch]
                        source_rows += len(rows)
                        if deferred_cols:
                            for row in rows:
                                update_values = {
                                    col: row[col]
                                    for col in deferred_cols
                                    if row.get(col) is not None
                                }
                                if update_values:
                                    pending.append((table, row["id"], update_values))
                                for col in deferred_cols:
                                    row[col] = None
                        inserted = dst_conn.execute(statement, rows)
                        inserted_rows += inserted.rowcount or 0
                        batch = mappings.fetchmany(batch_size)
    skipped_rows = source_rows - inserted_rows if not dry_run else 0
    return (
        TableMigrationResult(
            table_name=table.name,
            source_rows=source_rows,
            inserted_rows=inserted_rows,
            skipped_rows=skipped_rows,
            elapsed_seconds=monotonic() - started_at,
        ),
        pending,
    )


def _insert_ignore_statement(target: Engine, table: Table):
    pk_columns = [column.name for column in table.primary_key.columns]
    dialect = target.dialect.name
    if dialect == "sqlite":
        statement = sqlite_insert(table)
        return statement.on_conflict_do_nothing(index_elements=pk_columns)
    if dialect == "postgresql":
        statement = postgresql_insert(table)
        return statement.on_conflict_do_nothing(index_elements=pk_columns)
    return table.insert()


def _apply_deferred_updates(target: Engine, updates: Sequence[DeferredUpdate]) -> None:
    if not updates:
        return
    with target.begin() as conn:
        for table, pk, values in updates:
            conn.execute(table.update().where(table.c.id == pk).values(**values))


def main() -> None:
    """Ad-hoc CLI: ``python -m backend.scripts.migrate_data SRC DST``."""
    import json
    import sys

    if len(sys.argv) != 3:
        print(
            "usage: python -m backend.scripts.migrate_data SOURCE_URL TARGET_URL",
            file=sys.stderr,
        )
        sys.exit(2)
    report = migrate_data(sys.argv[1], sys.argv[2])
    print(json.dumps(report.as_dict(), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()

"""Fail-closed, read-only database inspection for operator diagnostics."""

from __future__ import annotations

import hashlib
import hmac
import os
import sqlite3
import stat
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO, Literal

import sqlalchemy as sa
from sqlalchemy.engine import Connection, make_url
from sqlalchemy.exc import ArgumentError
from sqlalchemy.pool import NullPool

from backend.services.ops.db_revision_inventory import (
    FINAL_EPIC1_0002_MARKERS,
    CheckpointProbeRequest,
    DatabaseCheckpointBinding,
    DatabaseSnapshot,
    LiveProbeRequest,
    MarkerSnapshot,
    ProbeFailure,
    RevisionProbeRequest,
    Sha256FileBinding,
    is_revision_id,
)

LIBRARY_0005_MARKERS = (
    # A-1: library_conversion_run/library_conversion_attempt are retired in
    # favor of the unified library_run/library_attempt (see
    # db_revision_inventory.FINAL_EPIC1_0002_MARKERS's own note).
    "table:library_run",
    "table:library_attempt",
    "table:library_projection_outbox",
    "column:library_item.current_ready_version_id",
    "column:library_item_version.source_blob_id",
    "column:library_item_version.selected_conversion_attempt_id",
    "column:library_artifact.blob_id",
)
CONTEXT_0006_MARKERS = ("column:review_item.decision_summary_code",)
CONTEXT_0008_MARKERS = (
    # P10.3 T1 (D-01v2/D-01v3) replaced the singular
    # api_tokens.workspace_id column with the api_token_workspaces
    # allowed-set table -- this table's existence is now the unambiguous
    # fingerprint of "this phase's migration ran", same role the retired
    # column used to play.
    "table:api_token_workspaces",
    "column:api_tokens.catalog_app_id",
    "column:api_tokens.catalog_app_label_snapshot",
    "column:api_tokens.catalog_revision",
    "column:oauth_clients.catalog_identity_fingerprint",
    "column:oauth_clients.catalog_app_id",
    "column:oauth_clients.catalog_app_label_snapshot",
    "column:oauth_clients.catalog_revision",
    "column:oauth_clients.catalog_association_provenance",
    "column:oauth_consents.catalog_app_id",
    "column:oauth_consents.catalog_app_label_snapshot",
    "column:oauth_consents.catalog_revision",
    "column:mcp_client.runtime_binding_key",
    "column:mcp_client.deleted_at",
    "table:workspace_credential_grant",
    "column:oauth_refresh_tokens.rotated_from_id",
)
MEDIA_0009_MARKERS = (
    "table:workspace_media_write_intent",
    "table:workspace_media_quota_usage",
    "table:media_staging_lease",
    "table:stored_blob",
    "table:blob_incarnation",
    "table:stored_blob_active_incarnation",
    "table:stored_blob_inspection",
    "table:media_physical_charge",
    "table:media_deletion_receipt",
    "table:media_install_request",
    "table:media_outbox",
    "table:security_blocked_blob",
    "table:context_media_asset",
    "table:context_media_usage",
    "table:context_media_upload_request",
    "table:context_media_gc_candidate",
)
PUBLIC_EXPOSURE_0010_MARKERS = (
    "column:context_page.public_exposure_scope",
    "column:context_page.public_exposed_version_id",
)
_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


class SqlAlchemyRevisionInventoryProbe:
    def probe(self, request: RevisionProbeRequest) -> DatabaseSnapshot:
        if isinstance(request, CheckpointProbeRequest):
            if (
                not isinstance(request.checkpoint, DatabaseCheckpointBinding)
                or not isinstance(request.checkpoint.path, Path)
                or not isinstance(request.checkpoint.expected_main, Sha256FileBinding)
            ):
                raise ProbeFailure(
                    "invalid_probe_request",
                    category="contract_violation",
                )
            return _probe_sqlite_path(
                request.checkpoint.path,
                checkpoint_binding=request.checkpoint.expected_main,
                timeout_seconds=5.0,
            )
        if not isinstance(request, LiveProbeRequest):
            raise ProbeFailure(
                "invalid_probe_request",
                category="contract_violation",
            )
        if not isinstance(request.target, (str, Path)):
            raise ProbeFailure(
                "invalid_probe_request",
                category="contract_violation",
            )
        if isinstance(request.target, Path):
            return _probe_sqlite_path(
                request.target,
                checkpoint_binding=None,
                timeout_seconds=5.0,
            )
        return probe_read_only(request.target)


@contextmanager
def _sqlite_probe_connection(connection: Connection) -> Iterator[Connection]:
    """Close a probe connection without replacing a primary probe failure."""

    try:
        yield connection
    except Exception:
        try:
            connection.close()
        except Exception:
            pass
        raise
    try:
        connection.close()
    except Exception as exc:
        raise ProbeFailure("database_probe_failed") from exc


def probe_read_only(
    database_url: str,
    *,
    timeout_seconds: float = 5.0,
) -> DatabaseSnapshot:
    """Inspect a supported database without using the application's write engine."""

    try:
        url = make_url(database_url)
    except ArgumentError as exc:
        raise ProbeFailure("invalid_database_url") from exc
    dialect = url.get_backend_name()
    if dialect == "sqlite":
        if url.drivername not in {"sqlite", "sqlite+pysqlite"}:
            raise ProbeFailure("unsupported_dialect")
        database = url.database
        if not database or database == ":memory:" or url.query.get("mode") == "memory":
            raise ProbeFailure("sqlite_file_required")
        return _probe_sqlite_path(
            Path(database),
            checkpoint_binding=None,
            timeout_seconds=timeout_seconds,
        )
    if dialect == "postgresql":
        if url.drivername != "postgresql+pg8000":
            raise ProbeFailure("unsupported_dialect")
        return _probe_postgresql(database_url, url, timeout_seconds=timeout_seconds)
    raise ProbeFailure("unsupported_dialect")


def _probe_sqlite_path(
    database: Path,
    *,
    checkpoint_binding: Sha256FileBinding | None,
    timeout_seconds: float,
) -> DatabaseSnapshot:
    requested_path = database.expanduser().absolute()
    _reject_link_components(requested_path)
    object_identity = _regular_file_identity(requested_path)
    try:
        path = requested_path.resolve(strict=True)
    except OSError as exc:
        raise ProbeFailure("sqlite_file_missing") from exc
    engine = None
    checkpoint_handle: BinaryIO | None = None
    checkpoint_identity: tuple[int, int, int, int] | None = None
    checkpoint_header: bytes | None = None
    wal_identities: tuple[tuple[int, int], tuple[int, int]] | None = None
    primary_failure: ProbeFailure | None = None
    try:
        if checkpoint_binding is not None:
            _validate_checkpoint_binding(checkpoint_binding)
            checkpoint_handle = _open_checkpoint_handle(path)
            checkpoint_identity = _checkpoint_handle_identity(checkpoint_handle)
            if _checkpoint_file_identity(path) != checkpoint_identity:
                raise ProbeFailure(
                    "sqlite_checkpoint_binding_unverifiable",
                    category="checkpoint_binding_unverifiable",
                )
            checkpoint_header = _require_standalone_sqlite_checkpoint(
                path,
                handle=checkpoint_handle,
            )
        else:
            wal_identities = _require_existing_wal_sidecars(path)

        uri = f"{path.as_uri()}?mode=ro"
        query_deadline = time.monotonic() + timeout_seconds

        def creator():
            connection = sqlite3.connect(
                uri,
                uri=True,
                timeout=timeout_seconds,
                check_same_thread=False,
            )

            def progress() -> int:
                return int(time.monotonic() >= query_deadline)

            connection.set_progress_handler(progress, 1000)
            return connection

        engine = sa.create_engine(
            "sqlite+pysqlite://",
            creator=creator,
            poolclass=NullPool,
            future=True,
        )
        with _sqlite_probe_connection(engine.connect()) as connection:
            try:
                connection.exec_driver_sql("PRAGMA query_only=ON")
                verified = (
                    connection.exec_driver_sql("PRAGMA query_only").scalar_one() == 1
                )
                if not verified:
                    raise ProbeFailure("read_only_not_verified")
                # BEGIN is deferred. The schema SELECT below is the real read
                # that acquires SHARED and pins the checkpoint before hashing.
                connection.exec_driver_sql("BEGIN")
                if checkpoint_binding is not None:
                    connection.exec_driver_sql(
                        "SELECT count(*) FROM sqlite_schema"
                    ).scalar_one()
                    _require_delete_journal_mode(connection)
                    assert checkpoint_handle is not None
                    assert checkpoint_identity is not None
                    assert checkpoint_header is not None
                    _verify_checkpoint_state(
                        requested_path=requested_path,
                        path=path,
                        object_identity=object_identity,
                        handle=checkpoint_handle,
                        checkpoint_identity=checkpoint_identity,
                        checkpoint_header=checkpoint_header,
                    )
                    _verify_checkpoint_digest(checkpoint_handle, checkpoint_binding)
                    # Hashing a large checkpoint is intentionally not governed by
                    # the database query timeout. Recheck the cheap filesystem
                    # invariants before inspecting through the pinned read view.
                    _verify_checkpoint_state(
                        requested_path=requested_path,
                        path=path,
                        object_identity=object_identity,
                        handle=checkpoint_handle,
                        checkpoint_identity=checkpoint_identity,
                        checkpoint_header=checkpoint_header,
                    )
                    query_deadline = time.monotonic() + timeout_seconds
                snapshot = _inspect(
                    connection,
                    dialect="sqlite",
                    locator={"dialect": "sqlite", "path": str(path)},
                    mechanism="sqlite_mode_ro_query_only_transaction",
                )
                if checkpoint_binding is not None:
                    _require_delete_journal_mode(connection)
                    _verify_checkpoint_state(
                        requested_path=requested_path,
                        path=path,
                        object_identity=object_identity,
                        handle=checkpoint_handle,
                        checkpoint_identity=checkpoint_identity,
                        checkpoint_header=checkpoint_header,
                    )
                else:
                    _verify_live_sqlite_state(
                        requested_path=requested_path,
                        path=path,
                        object_identity=object_identity,
                        wal_identities=wal_identities,
                    )
                return snapshot
            except Exception as exc:
                try:
                    if checkpoint_binding is not None:
                        assert checkpoint_handle is not None
                        assert checkpoint_identity is not None
                        assert checkpoint_header is not None
                        _verify_checkpoint_state(
                            requested_path=requested_path,
                            path=path,
                            object_identity=object_identity,
                            handle=checkpoint_handle,
                            checkpoint_identity=checkpoint_identity,
                            checkpoint_header=checkpoint_header,
                        )
                    else:
                        _verify_live_sqlite_state(
                            requested_path=requested_path,
                            path=path,
                            object_identity=object_identity,
                            wal_identities=wal_identities,
                        )
                except ProbeFailure as verification_failure:
                    raise verification_failure from exc
                if isinstance(exc, ProbeFailure):
                    raise
                raise ProbeFailure("database_probe_failed") from exc
    except ProbeFailure as exc:
        primary_failure = exc
        raise
    except Exception as exc:  # the raw driver error can contain sensitive paths
        primary_failure = ProbeFailure("database_probe_failed")
        raise primary_failure from exc
    finally:
        try:
            if engine is not None:
                engine.dispose()
        except Exception as exc:
            if primary_failure is None:
                raise ProbeFailure("database_probe_failed") from exc
        finally:
            if checkpoint_handle is not None:
                try:
                    checkpoint_handle.close()
                except OSError as exc:
                    if primary_failure is None:
                        raise ProbeFailure(
                            "sqlite_checkpoint_binding_unverifiable",
                            category="checkpoint_binding_unverifiable",
                        ) from exc


def _probe_postgresql(
    database_url: str, url, *, timeout_seconds: float
) -> DatabaseSnapshot:
    # NullPool prevents this specially constrained connection from being reused
    # by runtime code.
    engine = None
    transaction = None
    connection = None
    primary_failure: ProbeFailure | None = None
    try:
        timeout_ms = max(1, int(timeout_seconds * 1000))
        startup_options = (
            "-c default_transaction_read_only=on "
            f"-c statement_timeout={timeout_ms} -c lock_timeout={timeout_ms}"
        )
        engine = sa.create_engine(
            database_url,
            connect_args={
                "timeout": timeout_seconds,
                "startup_params": {"options": startup_options},
            },
            poolclass=NullPool,
            future=True,
        )
        connection = engine.connect()
        transaction = connection.begin()
        # This must be the first application statement in the transaction.
        connection.exec_driver_sql(
            "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"
        )
        verified = str(
            connection.exec_driver_sql("SHOW transaction_read_only").scalar_one()
        ).lower() in {"on", "true"}
        if not verified:
            raise ProbeFailure("read_only_not_verified")
        isolation = str(
            connection.exec_driver_sql("SHOW transaction_isolation").scalar_one()
        ).lower()
        if isolation != "repeatable read":
            raise ProbeFailure("isolation_not_verified")
        connection.exec_driver_sql(f"SET LOCAL statement_timeout = {timeout_ms}")
        connection.exec_driver_sql(f"SET LOCAL lock_timeout = {timeout_ms}")
        return _inspect(
            connection,
            dialect="postgresql",
            locator={
                "dialect": "postgresql",
                "host": url.host,
                "port": url.port,
                "database": url.database,
            },
            mechanism="postgresql_repeatable_read_transaction_read_only",
        )
    except ProbeFailure as exc:
        primary_failure = exc
        raise
    except Exception as exc:  # never surface driver repr/DSN
        primary_failure = ProbeFailure("database_probe_failed")
        raise primary_failure from exc
    finally:
        cleanup_error: Exception | None = None
        try:
            if transaction is not None and transaction.is_active:
                transaction.rollback()
        except Exception as exc:
            cleanup_error = exc
        try:
            if connection is not None:
                connection.close()
        except Exception as exc:
            if cleanup_error is None:
                cleanup_error = exc
        try:
            if engine is not None:
                engine.dispose()
        except Exception as exc:
            if cleanup_error is None:
                cleanup_error = exc
        if cleanup_error is not None and primary_failure is None:
            raise ProbeFailure("database_probe_failed") from cleanup_error


def inspect_revision_snapshot(connection: Connection) -> DatabaseSnapshot:
    """Inspect revision/schema evidence through an existing app connection.

    Bootstrap already owns a writable migration connection, so it cannot use
    the operator CLI's stricter file-level read-only adapter (a closed SQLite
    WAL database intentionally fails that adapter). This entry point performs
    only catalog reads and marks the snapshot as not independently read-only.
    """

    dialect_name = connection.dialect.name
    if dialect_name == "sqlite":
        dialect: Literal["sqlite", "postgresql"] = "sqlite"
    elif dialect_name == "postgresql":
        dialect = "postgresql"
    else:
        raise ProbeFailure("unsupported_dialect")
    return _inspect(
        connection,
        dialect=dialect,
        locator={"dialect": dialect},
        mechanism="application_connection_catalog_reads",
        read_only_verified=False,
    )


def _inspect(
    connection: Connection,
    *,
    dialect: Literal["sqlite", "postgresql"],
    locator: dict[str, object],
    mechanism: str,
    read_only_verified: bool = True,
) -> DatabaseSnapshot:
    inspector = sa.inspect(connection)
    has_alembic = inspector.has_table("alembic_version")
    revisions: tuple[str, ...] = ()
    if has_alembic:
        rows = connection.execute(
            sa.text("SELECT version_num FROM alembic_version ORDER BY version_num")
        ).scalars()
        collected = tuple(rows)
        if any(not is_revision_id(row) for row in collected):
            raise ProbeFailure("invalid_alembic_revision", category="invalid_revision")
        revisions = collected

    tables = set(inspector.get_table_names())
    columns: dict[str, set[str]] = {}

    def has_column(table: str, column: str) -> bool:
        if table not in tables:
            return False
        if table not in columns:
            columns[table] = {item["name"] for item in inspector.get_columns(table)}
        return column in columns[table]

    library_present = {
        marker
        for marker in LIBRARY_0005_MARKERS
        if _marker_present(marker, tables=tables, has_column=has_column)
    }
    context_0006_present = {
        marker
        for marker in CONTEXT_0006_MARKERS
        if _marker_present(marker, tables=tables, has_column=has_column)
    }
    context_0008_present = {
        marker
        for marker in CONTEXT_0008_MARKERS
        if _marker_present(marker, tables=tables, has_column=has_column)
    }
    media_0009_present = {
        marker
        for marker in MEDIA_0009_MARKERS
        if _marker_present(marker, tables=tables, has_column=has_column)
    }
    public_exposure_0010_present = {
        marker
        for marker in PUBLIC_EXPOSURE_0010_MARKERS
        if _marker_present(marker, tables=tables, has_column=has_column)
    }
    final_epic1_0002_present = {
        marker
        for marker in FINAL_EPIC1_0002_MARKERS
        if _marker_present(marker, tables=tables, has_column=has_column)
    }

    context_0005_marker = "fk:audit_event.workspace_id->workspace.id:on_delete_set_null"
    context_0005_present: set[str] = set()
    if "audit_event" in tables:
        if dialect == "sqlite":
            candidates = [
                row
                for row in connection.exec_driver_sql(
                    'PRAGMA foreign_key_list("audit_event")'
                ).mappings()
                if row["from"] == "workspace_id"
                and row["table"] == "workspace"
                and row["to"] == "id"
            ]
            ondelete = str(candidates[0]["on_delete"]) if len(candidates) == 1 else ""
        else:
            candidates = [
                fk
                for fk in inspector.get_foreign_keys("audit_event")
                if fk.get("constrained_columns") == ["workspace_id"]
                and fk.get("referred_table") == "workspace"
                and fk.get("referred_columns") == ["id"]
            ]
            ondelete = (
                str((candidates[0].get("options") or {}).get("ondelete") or "")
                if len(candidates) == 1
                else ""
            )
        if ondelete:
            normalized = " ".join(ondelete.upper().replace("_", " ").split())
            if normalized == "SET NULL":
                context_0005_present.add(context_0005_marker)

    return DatabaseSnapshot(
        dialect=dialect,
        locator=locator,
        read_only_mechanism=mechanism,
        read_only_verified=read_only_verified,
        alembic_table_present=has_alembic,
        alembic_revisions=revisions,
        library_0005=_marker_snapshot(LIBRARY_0005_MARKERS, library_present),
        context_0005=_marker_snapshot((context_0005_marker,), context_0005_present),
        context_0006=_marker_snapshot(CONTEXT_0006_MARKERS, context_0006_present),
        context_0008=_marker_snapshot(CONTEXT_0008_MARKERS, context_0008_present),
        media_0009=_marker_snapshot(MEDIA_0009_MARKERS, media_0009_present),
        public_exposure_0010=_marker_snapshot(
            PUBLIC_EXPOSURE_0010_MARKERS,
            public_exposure_0010_present,
        ),
        final_epic1_0002=_marker_snapshot(
            FINAL_EPIC1_0002_MARKERS,
            final_epic1_0002_present,
        ),
    )


def _marker_present(marker: str, *, tables: set[str], has_column) -> bool:
    kind, value = marker.split(":", 1)
    if kind == "table":
        return value in tables
    table, column = value.split(".", 1)
    return has_column(table, column)


def _marker_snapshot(expected: tuple[str, ...], present: set[str]) -> MarkerSnapshot:
    ordered_present = tuple(marker for marker in expected if marker in present)
    missing = tuple(marker for marker in expected if marker not in present)
    state: Literal["complete", "absent", "partial"]
    if not ordered_present:
        state = "absent"
    elif not missing:
        state = "complete"
    else:
        state = "partial"
    return MarkerSnapshot(state=state, present=ordered_present, missing=missing)


def _reject_link_components(path: Path) -> None:
    for candidate in (path, *path.parents):
        try:
            path_stat = candidate.lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise ProbeFailure("sqlite_file_inaccessible") from exc
        if stat.S_ISLNK(path_stat.st_mode) or bool(
            getattr(path_stat, "st_file_attributes", 0) & _REPARSE_POINT
        ):
            raise ProbeFailure("sqlite_symlink_refused")


def _regular_file_identity(path: Path) -> tuple[int, int]:
    try:
        path_stat = path.stat(follow_symlinks=False)
    except FileNotFoundError as exc:
        raise ProbeFailure("sqlite_file_missing") from exc
    except OSError as exc:
        raise ProbeFailure("sqlite_file_inaccessible") from exc
    if stat.S_ISLNK(path_stat.st_mode) or bool(
        getattr(path_stat, "st_file_attributes", 0) & _REPARSE_POINT
    ):
        raise ProbeFailure("sqlite_symlink_refused")
    if not stat.S_ISREG(path_stat.st_mode):
        raise ProbeFailure("sqlite_file_not_regular")
    return (path_stat.st_dev, path_stat.st_ino)


def _checkpoint_file_identity(path: Path) -> tuple[int, int, int, int]:
    try:
        path_stat = path.stat(follow_symlinks=False)
    except FileNotFoundError as exc:
        raise ProbeFailure(
            "sqlite_checkpoint_binding_unverifiable",
            category="checkpoint_binding_unverifiable",
        ) from exc
    except OSError as exc:
        raise ProbeFailure(
            "sqlite_checkpoint_binding_unverifiable",
            category="checkpoint_binding_unverifiable",
        ) from exc
    if stat.S_ISLNK(path_stat.st_mode) or bool(
        getattr(path_stat, "st_file_attributes", 0) & _REPARSE_POINT
    ):
        raise ProbeFailure("sqlite_symlink_refused")
    if not stat.S_ISREG(path_stat.st_mode):
        raise ProbeFailure("sqlite_file_not_regular")
    return (
        path_stat.st_dev,
        path_stat.st_ino,
        path_stat.st_size,
        path_stat.st_mtime_ns,
    )


def _checkpoint_handle_identity(handle: BinaryIO) -> tuple[int, int, int, int]:
    try:
        handle_stat = os.fstat(handle.fileno())
    except OSError as exc:
        raise ProbeFailure(
            "sqlite_checkpoint_binding_unverifiable",
            category="checkpoint_binding_unverifiable",
        ) from exc
    if not stat.S_ISREG(handle_stat.st_mode):
        raise ProbeFailure("sqlite_file_not_regular")
    return (
        handle_stat.st_dev,
        handle_stat.st_ino,
        handle_stat.st_size,
        handle_stat.st_mtime_ns,
    )


def _open_checkpoint_handle(path: Path) -> BinaryIO:
    descriptor: int | None = None
    try:
        flags = (
            os.O_RDONLY
            | getattr(os, "O_BINARY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        descriptor = os.open(path, flags)
        handle = os.fdopen(descriptor, "rb", buffering=0)
        descriptor = None
        return handle
    except OSError as exc:
        raise ProbeFailure(
            "sqlite_checkpoint_binding_unverifiable",
            category="checkpoint_binding_unverifiable",
        ) from exc
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass


def _require_existing_wal_sidecars(
    path: Path,
) -> tuple[tuple[int, int], tuple[int, int]] | None:
    header = _sqlite_header(path)
    if len(header) < 20 or header[:16] != b"SQLite format 3\x00":
        return None
    if header[18] != 2 and header[19] != 2:
        return None
    wal = Path(f"{path}-wal")
    shm = Path(f"{path}-shm")
    try:
        return (_regular_file_identity(wal), _regular_file_identity(shm))
    except ProbeFailure as exc:
        if exc.code not in {"sqlite_file_missing", "sqlite_file_not_regular"}:
            raise
        raise ProbeFailure(
            "sqlite_wal_sidecars_required",
            category="snapshot_incomplete",
        ) from exc


def _require_standalone_sqlite_checkpoint(
    path: Path,
    *,
    handle: BinaryIO | None = None,
) -> bytes:
    header = _sqlite_header(path, handle=handle)
    if (
        len(header) < 100
        or header[:16] != b"SQLite format 3\x00"
        or header[18:20] != b"\x01\x01"
    ):
        raise ProbeFailure(
            "sqlite_checkpoint_not_standalone",
            category="snapshot_incomplete",
        )
    for suffix in ("-wal", "-shm", "-journal"):
        sidecar = Path(f"{path}{suffix}")
        try:
            sidecar.lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise ProbeFailure("sqlite_file_inaccessible") from exc
        raise ProbeFailure(
            "sqlite_checkpoint_not_standalone",
            category="snapshot_incomplete",
        )
    return header


def _sqlite_header(path: Path, *, handle: BinaryIO | None = None) -> bytes:
    try:
        if handle is not None:
            handle.seek(0)
            return handle.read(100)
        with path.open("rb") as opened:
            return opened.read(100)
    except OSError as exc:
        raise ProbeFailure("sqlite_file_inaccessible") from exc


def _validate_checkpoint_binding(binding: Sha256FileBinding) -> None:
    if (
        not isinstance(binding.byte_size, int)
        or isinstance(binding.byte_size, bool)
        or binding.byte_size < 100
        or not isinstance(binding.hexdigest, str)
        or len(binding.hexdigest) != 64
        or binding.hexdigest.lower() != binding.hexdigest
        or any(
            character not in "0123456789abcdef" for character in binding.hexdigest
        )
    ):
        raise ProbeFailure(
            "invalid_checkpoint_binding",
            category="contract_violation",
        )


def _require_delete_journal_mode(connection: Connection) -> None:
    mode = str(
        connection.exec_driver_sql("PRAGMA journal_mode").scalar_one()
    ).lower()
    if mode != "delete":
        raise ProbeFailure(
            "sqlite_checkpoint_not_standalone",
            category="snapshot_incomplete",
        )


def _verify_checkpoint_digest(
    handle: BinaryIO,
    binding: Sha256FileBinding,
) -> None:
    digest = hashlib.sha256()
    total = 0
    try:
        handle.seek(0)
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            digest.update(chunk)
    except OSError as exc:
        raise ProbeFailure(
            "sqlite_checkpoint_digest_unverifiable",
            category="checkpoint_binding_unverifiable",
        ) from exc
    if total != binding.byte_size or not hmac.compare_digest(
        digest.hexdigest(), binding.hexdigest
    ):
        raise ProbeFailure(
            "sqlite_checkpoint_digest_mismatch",
            category="checkpoint_binding_mismatch",
        )


def _verify_checkpoint_state(
    *,
    requested_path: Path,
    path: Path,
    object_identity: tuple[int, int],
    handle: BinaryIO,
    checkpoint_identity: tuple[int, int, int, int],
    checkpoint_header: bytes,
) -> None:
    _reject_link_components(requested_path)
    if _regular_file_identity(requested_path) != object_identity:
        raise ProbeFailure(
            "sqlite_checkpoint_binding_unverifiable",
            category="checkpoint_binding_unverifiable",
        )
    if (
        _checkpoint_file_identity(path) != checkpoint_identity
        or _checkpoint_handle_identity(handle) != checkpoint_identity
    ):
        raise ProbeFailure(
            "sqlite_checkpoint_binding_unverifiable",
            category="checkpoint_binding_unverifiable",
        )
    if (
        _require_standalone_sqlite_checkpoint(path, handle=handle)
        != checkpoint_header
    ):
        raise ProbeFailure(
            "sqlite_checkpoint_binding_unverifiable",
            category="checkpoint_binding_unverifiable",
        )


def _verify_live_sqlite_state(
    *,
    requested_path: Path,
    path: Path,
    object_identity: tuple[int, int],
    wal_identities: tuple[tuple[int, int], tuple[int, int]] | None,
) -> None:
    _reject_link_components(requested_path)
    if _regular_file_identity(requested_path) != object_identity:
        raise ProbeFailure("sqlite_file_changed")
    if _require_existing_wal_sidecars(path) != wal_identities:
        raise ProbeFailure("sqlite_wal_sidecars_changed")

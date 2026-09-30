from __future__ import annotations

from importlib import import_module
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, text

consolidated = import_module(
    "backend.migrations.revision_0002.tenancy_schema"
)

pytestmark = pytest.mark.integration


def _seed_minimal_pre_final_schema(connection, *, cross_workspace: bool) -> None:
    # A-1: library_artifact now
    # covers both conversion and preview_generation artifacts in one table
    # (stage-discriminated), and library_conversion_attempt/
    # library_preview_attempt are unified into library_attempt the same
    # way -- see tenancy_schema._create_media_blob_ledger_and_
    # backfill()'s single library_artifact INSERT and its two
    # `JOIN library_attempt ... AND attempt.stage = ...` gc_hold INSERTs.
    for statement in (
        "CREATE TABLE blob_incarnation ("
        "workspace_id TEXT NOT NULL, blob_id TEXT NOT NULL, "
        "blob_epoch INTEGER NOT NULL, state TEXT NOT NULL, "
        "created_at TIMESTAMP NOT NULL, available_at TIMESTAMP, "
        "PRIMARY KEY (workspace_id, blob_id, blob_epoch))",
        "CREATE TABLE context_media_asset (workspace_id TEXT NOT NULL, "
        "id TEXT NOT NULL, "
        "blob_id TEXT NOT NULL, blob_epoch INTEGER NOT NULL)",
        "CREATE TABLE library_item_version (workspace_id TEXT NOT NULL, "
        "id TEXT NOT NULL, source_blob_workspace_id TEXT, source_blob_id TEXT, "
        "source_blob_epoch INTEGER)",
        "CREATE TABLE library_artifact (workspace_id TEXT NOT NULL, id TEXT NOT NULL, "
        "stage TEXT NOT NULL, "
        "blob_workspace_id TEXT, blob_id TEXT, blob_epoch INTEGER)",
        "CREATE TABLE media_staging_lease (workspace_id TEXT NOT NULL, "
        "id TEXT NOT NULL, generation INTEGER NOT NULL, actor_id TEXT NOT NULL, "
        "session_key TEXT NOT NULL)",
        "CREATE TABLE media_install_request (workspace_id TEXT NOT NULL, "
        "id TEXT NOT NULL, staging_id TEXT NOT NULL, staging_generation INTEGER "
        "NOT NULL, blob_id TEXT NOT NULL, blob_epoch INTEGER NOT NULL, "
        "state TEXT NOT NULL)",
        "CREATE TABLE library_attempt (workspace_id TEXT NOT NULL, "
        "id TEXT NOT NULL, stage TEXT NOT NULL, status TEXT NOT NULL, "
        "execution_generation INTEGER NOT NULL)",
    ):
        connection.execute(text(statement))

    workspace_id = "11111111111111111111111111111111"
    foreign_workspace_id = "22222222222222222222222222222222"
    blob_id = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    ownerless_blob_id = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    conversion_blob_id = "cccccccccccccccccccccccccccccccc"
    preview_blob_id = "dddddddddddddddddddddddddddddddd"
    bare_ownerless_blob_id = "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"
    connection.execute(
        text(
            "INSERT INTO blob_incarnation "
            "(workspace_id, blob_id, blob_epoch, state, created_at, available_at) "
            "VALUES (:workspace_id, :blob_id, 1, 'available', "
            "'2026-08-30 00:00:00', '2026-08-30 00:01:00'), "
            "(:workspace_id, :ownerless_blob_id, 1, 'available', "
            "'2026-08-30 00:02:00', '2026-08-30 00:03:00'), "
            "(:workspace_id, :conversion_blob_id, 1, 'available', "
            "'2026-08-30 00:04:00', '2026-08-30 00:05:00'), "
            "(:workspace_id, :preview_blob_id, 1, 'available', "
            "'2026-08-30 00:06:00', '2026-08-30 00:07:00'), "
            "(:workspace_id, :bare_ownerless_blob_id, 1, 'available', "
            "'2026-08-30 00:08:00', '2026-08-30 00:09:00')"
        ),
        {
            "workspace_id": workspace_id,
            "blob_id": blob_id,
            "ownerless_blob_id": ownerless_blob_id,
            "conversion_blob_id": conversion_blob_id,
            "preview_blob_id": preview_blob_id,
            "bare_ownerless_blob_id": bare_ownerless_blob_id,
        },
    )
    connection.execute(
        text(
            "INSERT INTO context_media_asset "
            "(workspace_id, id, blob_id, blob_epoch) "
            "VALUES (:workspace_id, '00000000000000000000000000000001', :blob_id, 1)"
        ),
        {"workspace_id": workspace_id, "blob_id": blob_id},
    )
    blob_workspace_id = foreign_workspace_id if cross_workspace else workspace_id
    connection.execute(
        text(
            "INSERT INTO library_item_version "
            "(workspace_id, id, source_blob_workspace_id, source_blob_id, "
            "source_blob_epoch) "
            "VALUES (:workspace_id, '00000000000000000000000000000002', "
            ":blob_workspace_id, :blob_id, 1)"
        ),
        {
            "workspace_id": workspace_id,
            "blob_workspace_id": blob_workspace_id,
            "blob_id": blob_id,
        },
    )
    # Two library_artifact rows, one per stage -- the A-1 unification's
    # equivalent of the old separate library_artifact/library_preview_
    # artifact rows, both of which the ledger backfill treats identically
    # (single owner_kind='library_artifact' INSERT regardless of stage).
    for owner_id, stage in (
        ("00000000000000000000000000000003", "conversion"),
        ("00000000000000000000000000000004", "preview_generation"),
    ):
        connection.execute(
            text(
                "INSERT INTO library_artifact "
                "(workspace_id, id, stage, blob_workspace_id, blob_id, blob_epoch) "
                "VALUES (:workspace_id, :owner_id, :stage, :blob_workspace_id, "
                ":blob_id, 1)"
            ),
            {
                "workspace_id": workspace_id,
                "owner_id": owner_id,
                "stage": stage,
                "blob_workspace_id": blob_workspace_id,
                "blob_id": blob_id,
            },
        )

    context_request_id = "10000000000000000000000000000001"
    context_staging_id = "20000000000000000000000000000001"
    conversion_attempt_id = "30000000000000000000000000000001"
    conversion_staging_id = "40000000000000000000000000000001"
    conversion_request_id = "50000000000000000000000000000001"
    preview_attempt_id = "60000000000000000000000000000001"
    preview_staging_id = "70000000000000000000000000000001"
    preview_request_id = "80000000000000000000000000000001"
    connection.execute(
        text(
            "INSERT INTO library_attempt "
            "(workspace_id, id, stage, status, execution_generation) VALUES "
            "(:workspace_id, :attempt_id, 'conversion', 'running', 3)"
        ),
        {"workspace_id": workspace_id, "attempt_id": conversion_attempt_id},
    )
    connection.execute(
        text(
            "INSERT INTO library_attempt "
            "(workspace_id, id, stage, status, execution_generation) VALUES "
            "(:workspace_id, :attempt_id, 'preview_generation', 'queued', 4)"
        ),
        {"workspace_id": workspace_id, "attempt_id": preview_attempt_id},
    )
    for staging_id, actor_id, session_key in (
        (context_staging_id, context_request_id, "context:owner"),
        (
            conversion_staging_id,
            conversion_attempt_id,
            f"library-conversion:{conversion_attempt_id}",
        ),
        (
            preview_staging_id,
            preview_attempt_id,
            f"library-preview:{preview_attempt_id}",
        ),
    ):
        connection.execute(
            text(
                "INSERT INTO media_staging_lease "
                "(workspace_id, id, generation, actor_id, session_key) VALUES "
                "(:workspace_id, :staging_id, 1, :actor_id, :session_key)"
            ),
            {
                "workspace_id": workspace_id,
                "staging_id": staging_id,
                "actor_id": actor_id,
                "session_key": session_key,
            },
        )
    for request_id, staging_id, installed_blob_id in (
        (context_request_id, context_staging_id, ownerless_blob_id),
        (conversion_request_id, conversion_staging_id, conversion_blob_id),
        (preview_request_id, preview_staging_id, preview_blob_id),
    ):
        connection.execute(
            text(
                "INSERT INTO media_install_request "
                "(workspace_id, id, staging_id, staging_generation, blob_id, "
                "blob_epoch, state) VALUES (:workspace_id, :request_id, "
                ":staging_id, 1, :blob_id, 1, 'available')"
            ),
            {
                "workspace_id": workspace_id,
                "request_id": request_id,
                "staging_id": staging_id,
                "blob_id": installed_blob_id,
            },
        )


def test_final_phase_backfills_all_owner_and_producer_kinds_and_downgrades(
    tmp_path: Path,
) -> None:
    # A-1 folded library_preview_artifact into library_artifact (stage-
    # discriminated), so there are 3 distinct owner_kind values now
    # (context_media_asset, library_item_version, library_artifact x2 rows)
    # instead of the former 4, though still 4 ledger rows and still both
    # producer_kind values (library_conversion_attempt/library_preview_
    # attempt) on the gc_hold side.
    engine = create_engine(f"sqlite+pysqlite:///{(tmp_path / 'ledger.db').as_posix()}")
    try:
        with engine.begin() as connection:
            _seed_minimal_pre_final_schema(connection, cross_workspace=False)
            context = MigrationContext.configure(connection)
            with Operations.context(context):
                consolidated._create_media_blob_ledger_and_backfill()

            rows = connection.execute(
                text(
                    "SELECT owner_kind, owner_id, blob_id, blob_epoch "
                    "FROM media_blob_reference ORDER BY owner_kind, owner_id"
                )
            ).all()
            # library_artifact appears twice -- one row per stage (see the
            # two rows _seed_minimal_pre_final_schema inserts, ids ...0003
            # and ...0004) -- because A-1 folded the former separate
            # library_preview_artifact source into the same table/owner_kind.
            assert [row.owner_kind for row in rows] == [
                "context_media_asset",
                "library_artifact",
                "library_artifact",
                "library_item_version",
            ]
            assert [row.owner_id for row in rows] == [
                "00000000000000000000000000000001",
                "00000000000000000000000000000003",
                "00000000000000000000000000000004",
                "00000000000000000000000000000002",
            ]
            assert {row.blob_id for row in rows} == {"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}
            assert {row.blob_epoch for row in rows} == {1}
            candidates = connection.execute(
                text(
                    "SELECT blob_id, reason, requested_at, eligible_at "
                    "FROM media_blob_gc_candidate ORDER BY blob_id"
                )
            ).all()
            assert [row.blob_id for row in candidates] == [
                "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
                "cccccccccccccccccccccccccccccccc",
                "dddddddddddddddddddddddddddddddd",
                "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee",
            ]
            assert {row.reason for row in candidates} == {"unowned_install"}
            assert all(row.eligible_at > row.requested_at for row in candidates)
            holds = connection.execute(
                text(
                    "SELECT producer_kind, producer_id, producer_generation, "
                    "blob_id, expires_at, "
                    "(julianday(expires_at) - julianday(created_at)) * 86400 "
                    "AS hold_seconds FROM media_blob_gc_hold "
                    "ORDER BY producer_kind"
                )
            ).all()
            assert [row.producer_kind for row in holds] == [
                "context_media_upload",
                "library_conversion_attempt",
                "library_preview_attempt",
            ]
            by_kind = {row.producer_kind: row for row in holds}
            assert by_kind["context_media_upload"].blob_id == (
                "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
            )
            assert by_kind["context_media_upload"].expires_at is not None
            assert by_kind["context_media_upload"].hold_seconds >= 3599.99
            assert by_kind["library_conversion_attempt"].producer_generation == 3
            assert by_kind["library_conversion_attempt"].blob_id == (
                "cccccccccccccccccccccccccccccccc"
            )
            assert by_kind["library_conversion_attempt"].expires_at is None
            assert by_kind["library_preview_attempt"].producer_generation == 4
            assert by_kind["library_preview_attempt"].blob_id == (
                "dddddddddddddddddddddddddddddddd"
            )
            assert by_kind["library_preview_attempt"].expires_at is None

            with Operations.context(context):
                consolidated._drop_media_blob_ledger()

        table_names = set(inspect(engine).get_table_names())
        assert "media_blob_reference" not in table_names
        assert "media_blob_gc_hold" not in table_names
        assert "media_blob_gc_candidate" not in table_names
    finally:
        engine.dispose()


def test_final_phase_rejects_cross_workspace_library_blob_before_ddl(
    tmp_path: Path,
) -> None:
    engine = create_engine(
        f"sqlite+pysqlite:///{(tmp_path / 'cross-workspace.db').as_posix()}"
    )
    try:
        with engine.begin() as connection:
            _seed_minimal_pre_final_schema(connection, cross_workspace=True)
            context = MigrationContext.configure(connection)
            with (
                Operations.context(context),
                pytest.raises(
                    RuntimeError,
                    match="media blob owner is cross-workspace",
                ),
            ):
                consolidated._create_media_blob_ledger_and_backfill()

        table_names = set(inspect(engine).get_table_names())
        assert "media_blob_reference" not in table_names
        assert "media_blob_gc_hold" not in table_names
        assert "media_blob_gc_candidate" not in table_names
    finally:
        engine.dispose()

"""Final tenancy constraints and preflight behavior of the final Epic-1 phase."""

from __future__ import annotations

from datetime import UTC, datetime
from importlib import import_module
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError

from backend.app.bootstrap import resolve_alembic_ini, resolve_migrations_dir
from backend.tests.utils.migrated_sqlite_templates import shared_templates
from backend.tests.utils.migration_paths import configure_test_repositories

final_phase = import_module(
    "backend.migrations.revision_modules.v0_1.efcbcf724889_core_schema.tenancy_schema"
)


def _config(database_url: str) -> Config:
    config = Config(str(resolve_alembic_ini()))
    config.set_main_option("script_location", str(resolve_migrations_dir()))
    config.set_main_option("sqlalchemy.url", database_url)
    configure_test_repositories(config)
    return config


def _run_steps(engine, *steps) -> None:
    with engine.begin() as connection:
        context = MigrationContext.configure(connection)
        with Operations.context(context):
            for step in steps:
                step()


@pytest.fixture(scope="session")
def sqlite_templates(tmp_path_factory):
    return shared_templates(tmp_path_factory)


def _clone_published_boundary(config: Config, templates) -> None:
    """Start from a copy of 0001 plus every phase before the tenancy phase."""
    database = make_url(config.get_main_option("sqlalchemy.url")).database
    templates.clone("boundary_pre_tenancy", Path(database))


def _upgrade_tenancy_schema(config: Config) -> None:
    engine = create_engine(config.get_main_option("sqlalchemy.url"))
    try:
        _run_steps(engine, final_phase.upgrade)
    finally:
        engine.dispose()


def _insert_workspace(connection, table, workspace_id: UUID, slug: str) -> None:
    now = datetime.now(UTC)
    actor_id = uuid4()
    values = {
        "id": workspace_id.hex,
        "slug": slug,
        "title": slug,
        "name": slug,
        "directory_title_key": slug,
        "description": None,
        "goal": "Migration coverage",
        "default_language": "en",
        "library_policy": {},
        "write_policy": {},
        "citation_policy": {},
        "agent_injection_policy": {},
        "review_policy": {},
        "lint_policy": {},
        "auto_fix_enabled": False,
        "visibility": "private",
        "created_by_actor_id": actor_id.hex,
        "owner_actor_id": actor_id.hex,
        "owner_user_id": None,
        "updated_at": now,
        "created_at": now,
    }
    connection.execute(table.insert().values(**values))


def _seed_two_workspaces(engine):
    metadata = sa.MetaData()
    workspaces = sa.Table("workspace", metadata, autoload_with=engine)
    items = sa.Table("library_item", metadata, autoload_with=engine)
    versions = sa.Table("library_item_version", metadata, autoload_with=engine)
    intents = sa.Table("workspace_media_write_intent", metadata, autoload_with=engine)
    blobs = sa.Table("stored_blob", metadata, autoload_with=engine)
    incarnations = sa.Table("blob_incarnation", metadata, autoload_with=engine)

    now = datetime.now(UTC)
    ws_a, ws_b = uuid4(), uuid4()
    item_a, item_b = uuid4(), uuid4()
    version_a, version_b = uuid4(), uuid4()
    blob_b = uuid4()
    with engine.begin() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=ON")
        _insert_workspace(connection, workspaces, ws_a, "phase-c-repair-a")
        _insert_workspace(connection, workspaces, ws_b, "phase-c-repair-b")
        for item_id, workspace_id, uri in (
            (item_a, ws_a, "https://example.test/a"),
            (item_b, ws_b, "https://example.test/b"),
        ):
            connection.execute(
                items.insert().values(
                    id=item_id.hex,
                    workspace_id=workspace_id.hex,
                    connector_type="web",
                    format_hint="unknown",
                    uri=uri,
                    metadata={},
                    trust_level="unknown",
                    lifecycle_status="registered",
                    updated_at=now,
                    created_at=now,
                )
            )
        for version_id, item_id, _workspace_id, marker in (
            (version_a, item_a, ws_a, "a"),
            (version_b, item_b, ws_b, "b"),
        ):
            connection.execute(
                versions.insert().values(
                    id=version_id.hex,
                    library_item_id=item_id.hex,
                    version_no=1,
                    content_hash=marker * 64,
                    raw_storage_ref=f"inline://{marker}",
                    processing_status="completed",
                    preview_status="unavailable",
                    fetched_at=now,
                    updated_at=now,
                    created_at=now,
                )
            )
        connection.execute(
            items.update()
            .where(items.c.id == item_a.hex)
            .values(current_version_id=version_a.hex)
        )
        connection.execute(
            intents.insert().values(
                workspace_id=ws_b.hex,
                state="open",
                delete_generation=0,
                updated_at=now,
            )
        )
        connection.execute(
            blobs.insert().values(
                workspace_id=ws_b.hex,
                id=blob_b.hex,
                sha256="c" * 64,
                next_epoch=2,
                created_at=now,
                updated_at=now,
            )
        )
        connection.execute(
            incarnations.insert().values(
                workspace_id=ws_b.hex,
                blob_id=blob_b.hex,
                blob_epoch=1,
                byte_size=1,
                storage_key=f"w/{ws_b.hex}/c/{blob_b.hex}",
                state="available",
                publish_generation=1,
                security_generation=0,
                gc_generation=0,
                created_at=now,
                available_at=now,
            )
        )
    return ws_a, ws_b, item_a, item_b, version_a, version_b, blob_b


def test_final_constraints_reject_cross_workspace_pointers(
    tmp_path: Path, sqlite_templates
):
    database_url = f"sqlite:///{tmp_path / 'tenancy-final-constraints.db'}"
    config = _config(database_url)
    _clone_published_boundary(config, sqlite_templates)
    engine = create_engine(database_url)
    ws_a, ws_b, _, item_b, version_a, _, blob_b = _seed_two_workspaces(engine)

    _upgrade_tenancy_schema(config)
    engine.dispose()
    engine = create_engine(database_url)
    items = sa.Table("library_item", sa.MetaData(), autoload_with=engine)
    versions = sa.Table("library_item_version", sa.MetaData(), autoload_with=engine)
    artifacts = sa.Table("library_artifact", sa.MetaData(), autoload_with=engine)
    runs = sa.Table("library_run", sa.MetaData(), autoload_with=engine)
    attempts = sa.Table("library_attempt", sa.MetaData(), autoload_with=engine)

    try:
        with pytest.raises(IntegrityError):
            with engine.begin() as connection:
                connection.exec_driver_sql("PRAGMA foreign_keys=ON")
                connection.execute(
                    items.update()
                    .where(items.c.id == item_b.hex)
                    .values(current_version_id=version_a.hex)
                )

        with pytest.raises(IntegrityError):
            with engine.begin() as connection:
                connection.exec_driver_sql("PRAGMA foreign_keys=ON")
                connection.execute(
                    versions.update()
                    .where(versions.c.id == version_a.hex)
                    .values(
                        source_blob_workspace_id=ws_b.hex,
                        source_blob_id=blob_b.hex,
                        source_blob_epoch=1,
                        original_filename="foreign.txt",
                        detected_media_type="text/plain",
                        format_id="txt",
                        byte_size=1,
                        detection_json={},
                    )
                )

        # A-1: library_artifact now
        # requires a real attempt (attempt_id is mandatory; the legacy
        # nullable-attempt shape is retired), so a valid, same-workspace
        # library_run/library_attempt pair is seeded first -- the only thing
        # under test is the blob-workspace mismatch, not an incidental FK
        # failure on attempt_id.
        run_id = uuid4()
        attempt_id = uuid4()
        now = datetime.now(UTC)
        with engine.begin() as connection:
            connection.exec_driver_sql("PRAGMA foreign_keys=ON")
            connection.execute(
                runs.insert().values(
                    id=run_id.hex,
                    workspace_id=ws_a.hex,
                    library_item_version_id=version_a.hex,
                    stage="conversion",
                    run_seq=1,
                    reason="initial",
                    status="queued",
                    provider_chain_json=[],
                    policy_revision="v1",
                    output_schema_version="v1",
                    idempotency_key="phase-c-repair-conversion",
                    updated_at=now,
                    created_at=now,
                )
            )
            connection.execute(
                attempts.insert().values(
                    id=attempt_id.hex,
                    workspace_id=ws_a.hex,
                    run_id=run_id.hex,
                    library_item_version_id=version_a.hex,
                    stage="conversion",
                    provider_id="provider",
                    provider_version="1",
                    provider_spi_version="1",
                    config_hash="config",
                    input_sha256="e" * 64,
                    output_schema_version="v1",
                    status="queued",
                    chain_index=0,
                    transport_retry_count=0,
                    execution_generation=1,
                    warnings_json=[],
                    provenance_json={},
                    metrics_json={},
                    created_at=now,
                )
            )

        with pytest.raises(IntegrityError):
            with engine.begin() as connection:
                connection.exec_driver_sql("PRAGMA foreign_keys=ON")
                connection.execute(
                    artifacts.insert().values(
                        id=uuid4().hex,
                        workspace_id=ws_a.hex,
                        library_item_version_id=version_a.hex,
                        attempt_id=attempt_id.hex,
                        stage="conversion",
                        blob_workspace_id=ws_b.hex,
                        blob_id=blob_b.hex,
                        blob_epoch=1,
                        role="markdown",
                        kind="markdown",
                        media_type="text/markdown",
                        content_sha256="f" * 64,
                        byte_size=1,
                        ordinal=0,
                        created_at=datetime.now(UTC),
                    )
                )

        # A-1 folded the former separate library_preview_artifact table (and
        # its own library_preview_artifact_blob_workspace_check) into this
        # same library_artifact / library_artifact_blob_workspace_check --
        # one physical CHECK now covers both stages. Its text, read directly
        # from library_media_schema.py, is the flat equality
        # "blob_workspace_id = workspace_id" with no `stage`-conditioned
        # clause at all, so it is stage-blind by construction: the
        # conversion-stage insert above already exercises the exact same
        # constraint object a preview-stage insert would hit, and a second
        # insert would only re-confirm SQLite re-evaluates one CHECK per row
        # (not something specific to this table). No second insert needed.
        artifact_checks = {
            check["name"]
            for check in inspect(engine).get_check_constraints("library_artifact")
        }
        assert "library_artifact_blob_workspace_check" in artifact_checks
    finally:
        engine.dispose()


def test_final_preflight_rejects_existing_cross_workspace_current_pointer(
    tmp_path: Path, sqlite_templates
):
    database_url = f"sqlite:///{tmp_path / 'tenancy-current-pointer-preflight.db'}"
    config = _config(database_url)
    _clone_published_boundary(config, sqlite_templates)
    engine = create_engine(database_url)
    _, _, _, item_b, version_a, _, _ = _seed_two_workspaces(engine)
    with engine.begin() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=ON")
        items = sa.Table("library_item", sa.MetaData(), autoload_with=engine)
        connection.execute(
            items.update()
            .where(items.c.id == item_b.hex)
            .values(current_version_id=version_a.hex)
        )
    engine.dispose()

    with pytest.raises(RuntimeError, match="current_version_id"):
        _upgrade_tenancy_schema(config)


def test_final_preflight_rejects_existing_blob_workspace_mismatch(
    tmp_path: Path, sqlite_templates
):
    database_url = f"sqlite:///{tmp_path / 'tenancy-blob-preflight.db'}"
    config = _config(database_url)
    _clone_published_boundary(config, sqlite_templates)
    engine = create_engine(database_url)
    _, ws_b, _, _, version_a, _, blob_b = _seed_two_workspaces(engine)
    with engine.begin() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=ON")
        versions = sa.Table("library_item_version", sa.MetaData(), autoload_with=engine)
        connection.execute(
            versions.update()
            .where(versions.c.id == version_a.hex)
            .values(
                source_blob_workspace_id=ws_b.hex,
                source_blob_id=blob_b.hex,
                source_blob_epoch=1,
                original_filename="foreign.txt",
                detected_media_type="text/plain",
                format_id="txt",
                byte_size=1,
                detection_json={},
            )
        )
    engine.dispose()

    with pytest.raises(RuntimeError, match="source_blob_workspace_id"):
        _upgrade_tenancy_schema(config)


def test_workspace_scoped_set_null_fk_does_not_null_the_tenant_column(
    tmp_path: Path, sqlite_templates
):
    """Reviewer-caught (79420555..3ea8580d review, handoff #2): a composite
    (workspace_id, pointer) FK with ON DELETE SET NULL nulls EVERY column of
    the composite on cascade -- including workspace_id, which is NOT NULL --
    so deleting a referenced row always failed with a confusing
    ``NOT NULL constraint failed: agent_page.workspace_id`` instead of a
    clean FK error. The final phase no longer sets ondelete on any affected
    composite FKs; real callers (workspace_write.py's
    _delete_seed_workspace, context_lifecycle.py, review_service.py) were
    already clearing the pointer explicitly before deleting, so nothing
    that previously worked relies on the DB-level cascade.
    """
    from backend.app.bootstrap import bootstrap
    from backend.app.db import make_session_factory_from_settings
    from backend.app.settings import Settings
    from backend.models.agent import AgentPage, AgentPageVersion
    from backend.scripts.seed_dev import seed_default_workspace

    # A migrated head schema (not create_all, whose FKs differ) with no version
    # marker yet, so bootstrap() only has to record it.
    database = tmp_path / "set-null-fk.sqlite"
    sqlite_templates.clone("head", database)
    settings = Settings(database_url=f"sqlite+pysqlite:///{database}")
    bootstrap(settings)
    factory = make_session_factory_from_settings(settings)

    with factory.begin() as session:
        ws = seed_default_workspace(session)
        page = AgentPage(
            workspace_id=ws.id,
            slug="set-null-fk-regression",
            title="Set Null FK Regression",
            category="persona",
            priority=0,
            lifecycle_status="active",
            tags=[],
        )
        session.add(page)
        session.flush()
        version = AgentPageVersion(
            workspace_id=ws.id,
            agent_page_id=page.id,
            version_no=1,
            body_markdown="body",
            actor_type="system",
        )
        session.add(version)
        session.flush()
        page.current_active_version_id = version.id
        session.flush()
        page_id = page.id
        version_id = version.id

    # Deleting a version a page still points to must fail cleanly -- a plain
    # FK violation, not a NOT NULL violation on an unrelated column -- until
    # the caller clears the pointer first (the pattern every real deletion
    # path already follows).
    with pytest.raises(IntegrityError, match="FOREIGN KEY constraint failed"):
        with factory.begin() as session:
            session.delete(session.get(AgentPageVersion, version_id))

    # Clearing the pointer first (the real _delete_seed_workspace pattern)
    # lets the delete through cleanly.
    with factory.begin() as session:
        session.get(AgentPage, page_id).current_active_version_id = None
    with factory.begin() as session:
        session.delete(session.get(AgentPageVersion, version_id))
    with factory() as session:
        assert session.get(AgentPageVersion, version_id) is None

"""SQLite migration and SQLAlchemy adapter gates for Media I3a."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from importlib import import_module
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, event, inspect, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from backend.app.bootstrap import resolve_alembic_ini, resolve_migrations_dir
from backend.media.adapters.sqlalchemy import SqlAlchemyMediaUnitOfWorkFactory
from backend.media.adapters.sqlalchemy.available import (
    SqlAlchemyAvailableBlobRepository,
)
from backend.media.adapters.sqlalchemy.quota import SqlAlchemyPhysicalQuota
from backend.media.adapters.sqlalchemy.repository import SqlAlchemyMediaRepository
from backend.media.application import RegisterAvailableBlob
from backend.media.domain import (
    AvailableBlobCandidate,
    BlobState,
    Sha256Digest,
    StoredBlob,
    WorkspaceBlobIdentity,
)
from backend.media.ports.contracts import (
    IdempotencyConflictError,
    PhysicalChargeTarget,
    PhysicalDeleteDisposition,
    PhysicalDeleteResult,
    PhysicalObjectKind,
    StorageKey,
)
from backend.media.ports.coordination import OutboxMessage
from backend.models.context_media import ContextMediaAsset
from backend.models.media import (
    BlobIncarnationRow,
    MediaDeletionReceipt,
    MediaPhysicalCharge,
    MediaStagingLease,
    StoredBlobActiveIncarnationRow,
    StoredBlobInspectionRow,
    StoredBlobRow,
    WorkspaceMediaQuotaUsage,
    WorkspaceMediaWriteIntent,
)
from backend.models.workspace import Workspace
from backend.tests.integration import test_context_media_repository as media_fixtures

epic1 = import_module("backend.migrations.versions.0002_epic1")
workspace_phase = import_module(
    "backend.migrations.revision_0002.workspace_schema"
)
mcp_phase = import_module("backend.migrations.revision_0002.identity_access_schema")
context_phase = import_module("backend.migrations.revision_0002.context_schema")
media_phase = import_module("backend.migrations.revision_0002.library_media_schema")

MEDIA_TABLES = {
    "workspace_media_write_intent",
    "workspace_media_quota_usage",
    "media_staging_lease",
    "stored_blob",
    "blob_incarnation",
    "stored_blob_active_incarnation",
    "stored_blob_inspection",
    "media_physical_charge",
    "media_deletion_receipt",
    "media_install_request",
    "media_outbox",
    "security_blocked_blob",
    # A-1: library_conversion_run/
    # attempt is now the unified library_run/library_attempt.
    "library_run",
    "library_attempt",
    "library_projection_outbox",
    "context_media_asset",
    "context_media_usage",
    "context_media_upload_request",
    "context_media_gc_candidate",
}


def _config(database_url: str) -> Config:
    config = Config(str(resolve_alembic_ini()))
    config.set_main_option("script_location", str(resolve_migrations_dir()))
    config.set_main_option("sqlalchemy.url", database_url)
    return config


def _database_url(tmp_path: Path, name: str) -> str:
    return f"sqlite:///{(tmp_path / name).as_posix()}"


def _engine(database_url: str) -> Engine:
    engine = create_engine(database_url)

    @event.listens_for(engine, "connect")
    def _enable_sqlite_foreign_keys(dbapi_connection, _connection_record) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    return engine


def _run_steps(engine: Engine, *steps) -> None:
    with engine.begin() as connection:
        context = MigrationContext.configure(connection)
        with Operations.context(context):
            for step in steps:
                step()


def _upgrade_to_media_boundary(config: Config, engine: Engine) -> None:
    command.upgrade(config, "0001_initial_release")
    _run_steps(
        engine,
        epic1._upgrade_base_schema,
        workspace_phase.upgrade,
        mcp_phase.upgrade,
        context_phase.upgrade,
    )


@pytest.fixture
def media_engine(tmp_path: Path) -> Engine:
    database_url = _database_url(tmp_path, "media.db")
    command.upgrade(_config(database_url), "head")
    engine = _engine(database_url)
    try:
        yield engine
    finally:
        engine.dispose()


def _create_workspace(session: Session, label: str) -> UUID:
    workspace_id = uuid4()
    session.add(
        Workspace(
            id=workspace_id,
            slug=f"media-{label}-{workspace_id.hex[:8]}",
            title=f"Media {label}",
            name=f"Media {label}",
        )
    )
    session.commit()
    return workspace_id


def _configure_media(
    session: Session,
    workspace_id: UUID,
    *,
    limit_bytes: int = 100,
) -> None:
    now = datetime.now(UTC)
    session.add(
        WorkspaceMediaWriteIntent(
            workspace_id=workspace_id,
            state="open",
            delete_generation=0,
            updated_at=now,
        )
    )
    session.flush()
    session.add(
        WorkspaceMediaQuotaUsage(
            workspace_id=workspace_id,
            limit_bytes=limit_bytes,
            charged_bytes=0,
            revision=0,
            updated_at=now,
        )
    )
    session.commit()


def _create_staging_lease(
    session: Session,
    workspace_id: UUID,
    *,
    byte_size: int,
) -> tuple[UUID, StorageKey]:
    staging_id = uuid4()
    key = StorageKey(f"workspaces/{workspace_id}/staging/{staging_id}")
    now = datetime.now(UTC)
    session.add(
        MediaStagingLease(
            workspace_id=workspace_id,
            id=staging_id,
            generation=1,
            actor_id=uuid4(),
            session_key=f"session:{uuid4()}",
            idempotency_key=f"stage:{uuid4()}",
            storage_key=key.value,
            max_bytes=byte_size,
            byte_size=byte_size,
            sha256="a" * 64,
            state="staged",
            expires_at=now + timedelta(hours=1),
            created_at=now,
            updated_at=now,
        )
    )
    session.commit()
    return staging_id, key


def test_media_phase_upgrades_from_mcp_boundary_and_downgrades_cleanly(
    tmp_path: Path,
) -> None:
    database_url = _database_url(tmp_path, "upgrade.db")
    config = _config(database_url)
    engine = _engine(database_url)
    try:
        _upgrade_to_media_boundary(config, engine)
        assert MEDIA_TABLES.isdisjoint(inspect(engine).get_table_names())
        _run_steps(engine, media_phase.upgrade)
        with engine.connect() as connection:
            assert connection.scalar(
                text("SELECT version_num FROM alembic_version")
            ) == "0001_initial_release"
        assert MEDIA_TABLES <= set(inspect(engine).get_table_names())

        _run_steps(
            engine,
            media_phase.prepare_downgrade_data,
            media_phase.downgrade,
        )
        assert MEDIA_TABLES.isdisjoint(inspect(engine).get_table_names())
    finally:
        engine.dispose()


def test_media_phase_downgrade_refuses_populated_context_media(tmp_path: Path) -> None:
    """P3.1: ContextMediaSession is gone (page-first only now), but the same
    downgrade guard (_refuse_lossy_integrated_media_downgrade) still checks
    context_media_asset for populated rows, so populate that table instead --
    a page, one of its own versions, and a ContextMediaAsset row pointed at
    them.

    context_page/context_page_version are seeded with raw SQL matched to
    THIS migration boundary's own columns rather than the current
    ContextPage/ContextPageVersion ORM models, which include columns (e.g.
    context_page.public_exposure_scope) added by phases that run after this
    one -- inserting through the current models here would write columns
    that do not exist yet at this point in the phase sequence.

    PRAGMA foreign_keys is turned off only to seed the fixture's reciprocal
    page/current-version references in one transaction. Context schema is
    already in its final workspace-scoped form at this domain boundary.
    """

    database_url = _database_url(tmp_path, "context-downgrade.db")
    config = _config(database_url)
    engine = _engine(database_url)
    try:
        _upgrade_to_media_boundary(config, engine)
        _run_steps(engine, media_phase.upgrade)
        now = media_fixtures.NOW
        page_id = uuid4()
        version_id = uuid4()
        asset_id = uuid4()
        with Session(engine) as session:
            raw_cursor = session.connection().connection.cursor()
            raw_cursor.execute("PRAGMA foreign_keys=OFF")
            raw_cursor.close()

            workspace_id = _create_workspace(session, "downgrade")
            blob, inspection_id = media_fixtures._seed_shared_image(
                session, workspace_id
            )
            session.execute(
                text(
                    "INSERT INTO context_page ("
                    "workspace_id, slug, title, current_version_id, page_type, "
                    "tags, aliases, lifecycle_status, is_public, updated_at, "
                    "id, created_at, stale_candidate"
                    ") VALUES ("
                    ":workspace_id, :slug, :title, :current_version_id, "
                    ":page_type, :tags, :aliases, :lifecycle_status, "
                    ":is_public, :updated_at, :id, :created_at, :stale_candidate"
                    ")"
                ),
                {
                    "workspace_id": workspace_id.hex,
                    "slug": "context-media-downgrade",
                    "title": "Context media downgrade fixture",
                    "current_version_id": version_id.hex,
                    "page_type": "overview",
                    "tags": "[]",
                    "aliases": "[]",
                    "lifecycle_status": "draft",
                    "is_public": 0,
                    "updated_at": now,
                    "id": page_id.hex,
                    "created_at": now,
                    "stale_candidate": 0,
                },
            )
            session.execute(
                text(
                    "INSERT INTO context_page_version ("
                    "workspace_id, page_id, version_no, body_md, rendering_profile, "
                    "content_hash, graph_status, actor_type, version_status, "
                    "created_by_actor_kind, created_by_actor_id, id, created_at"
                    ") VALUES ("
                    ":workspace_id, :page_id, :version_no, :body_md, "
                    ":rendering_profile, "
                    ":content_hash, :graph_status, :actor_type, "
                    ":version_status, :created_by_actor_kind, "
                    ":created_by_actor_id, :id, :created_at"
                    ")"
                ),
                {
                    "workspace_id": workspace_id.hex,
                    "page_id": page_id.hex,
                    "version_no": 1,
                    "body_md": "",
                    "rendering_profile": "gfm_mermaid",
                    "content_hash": "0" * 64,
                    "graph_status": "linear",
                    "actor_type": "human",
                    "version_status": "draft",
                    "created_by_actor_kind": "human",
                    "created_by_actor_id": "downgrade-fixture",
                    "id": version_id.hex,
                    "created_at": now,
                },
            )
            session.add(
                ContextMediaAsset(
                    workspace_id=workspace_id,
                    id=asset_id,
                    page_id=page_id,
                    created_for_version_id=version_id,
                    blob_id=blob.blob_id,
                    blob_epoch=blob.epoch.value,
                    inspection_id=inspection_id,
                    original_filename="pixel.png",
                    canonical_media_type="image/png",
                    logical_bytes=68,
                    policy_revision="context-mvp-v1",
                    created_by_actor_kind="human",
                    created_by_actor_id="downgrade-fixture",
                )
            )
            session.commit()

        with pytest.raises(RuntimeError, match="Context or Shared Media state"):
            _run_steps(
                engine,
                media_phase.prepare_downgrade_data,
                media_phase.downgrade,
            )

        with engine.connect() as connection:
            assert connection.scalar(
                text("SELECT version_num FROM alembic_version")
            ) == "0001_initial_release"
            assert connection.scalar(
                text(
                    "SELECT COUNT(*) FROM context_media_asset WHERE id = :id"
                ),
                {"id": asset_id.hex},
            ) == 1
    finally:
        engine.dispose()


def test_workspace_digest_is_unique_only_inside_one_workspace(
    media_engine: Engine,
) -> None:
    with Session(media_engine) as session:
        left = _create_workspace(session, "left")
        right = _create_workspace(session, "right")
        _configure_media(session, left)
        _configure_media(session, right)
        digest = "b" * 64
        session.add_all(
            [
                StoredBlobRow(
                    workspace_id=left,
                    id=uuid4(),
                    sha256=digest,
                    next_epoch=1,
                ),
                StoredBlobRow(
                    workspace_id=right,
                    id=uuid4(),
                    sha256=digest,
                    next_epoch=1,
                ),
            ]
        )
        session.commit()

        session.add(
            StoredBlobRow(
                workspace_id=left,
                id=uuid4(),
                sha256=digest,
                next_epoch=1,
            )
        )
        with pytest.raises(IntegrityError):
            session.commit()


def test_available_blob_registration_reuses_workspace_digest_and_charge(
    media_engine: Engine,
) -> None:
    with Session(media_engine) as session:
        workspace_id = _create_workspace(session, "available")
        _configure_media(session, workspace_id)
        repository = SqlAlchemyAvailableBlobRepository(
            session,
            media_repository=SqlAlchemyMediaRepository(session),
            physical_quota=SqlAlchemyPhysicalQuota(session),
        )
        candidate = AvailableBlobCandidate(
            workspace_id=workspace_id,
            sha256=Sha256Digest("9" * 64),
            byte_size=4,
            storage_key=f"w/{workspace_id.hex}/99/{'9' * 64}",
            canonical_media_type="image/png",
            format_id="png",
            detector_id="context-image-signature",
            detector_version="1",
            registry_version="context-media-v1",
            validation_schema_version="context-media-v1",
            metadata={},
        )

        first = RegisterAvailableBlob().execute(repository, candidate)
        second = RegisterAvailableBlob().execute(repository, candidate)
        session.commit()

        assert second == first
        assert session.scalar(
            select(StoredBlobRow).where(
                StoredBlobRow.workspace_id == workspace_id,
                StoredBlobRow.sha256 == candidate.sha256.value,
            )
        ) is not None
        usage = session.get(WorkspaceMediaQuotaUsage, workspace_id)
        assert usage is not None
        assert usage.charged_bytes == candidate.byte_size


def test_active_pointer_rejects_cross_workspace_incarnation(
    media_engine: Engine,
) -> None:
    with Session(media_engine) as session:
        left = _create_workspace(session, "left-pointer")
        right = _create_workspace(session, "right-pointer")
        _configure_media(session, left)
        _configure_media(session, right)
        blob_id = uuid4()
        now = datetime.now(UTC)
        session.add(
            StoredBlobRow(
                workspace_id=left,
                id=blob_id,
                sha256="c" * 64,
                next_epoch=2,
            )
        )
        session.flush()
        session.add(
            BlobIncarnationRow(
                workspace_id=left,
                blob_id=blob_id,
                blob_epoch=1,
                byte_size=5,
                storage_key=f"workspaces/{left}/{blob_id}/1/object",
                state="pending_install",
                publish_generation=0,
                security_generation=0,
                gc_generation=0,
                created_at=now,
            )
        )
        session.commit()

        session.add(
            StoredBlobActiveIncarnationRow(
                workspace_id=right,
                blob_id=blob_id,
                blob_epoch=1,
                activated_at=now,
                updated_at=now,
            )
        )
        with pytest.raises(IntegrityError):
            session.commit()


def test_workspace_delete_is_restricted_until_media_barrier_is_drained(
    media_engine: Engine,
) -> None:
    with Session(media_engine) as session:
        workspace_id = _create_workspace(session, "delete")
        _configure_media(session, workspace_id)
        workspace = session.get(Workspace, workspace_id)
        assert workspace is not None
        session.delete(workspace)
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()

        intent = session.get(WorkspaceMediaWriteIntent, workspace_id)
        usage = session.get(WorkspaceMediaQuotaUsage, workspace_id)
        assert intent is not None and usage is not None
        intent.state = "deleting"
        intent.delete_generation += 1
        session.delete(usage)
        session.flush()
        session.delete(intent)
        session.flush()
        workspace = session.get(Workspace, workspace_id)
        assert workspace is not None
        session.delete(workspace)
        session.commit()


def test_inspection_is_immutable_until_terminal_delete_barrier(
    media_engine: Engine,
) -> None:
    with Session(media_engine) as session:
        workspace_id = _create_workspace(session, "inspection")
        _configure_media(session, workspace_id)
        blob_id = uuid4()
        inspection_id = uuid4()
        now = datetime.now(UTC)
        session.add(
            StoredBlobRow(
                workspace_id=workspace_id,
                id=blob_id,
                sha256="d" * 64,
                next_epoch=2,
            )
        )
        session.flush()
        session.add(
            BlobIncarnationRow(
                workspace_id=workspace_id,
                blob_id=blob_id,
                blob_epoch=1,
                byte_size=1,
                storage_key=f"workspaces/{workspace_id}/{blob_id}/1/object",
                state="pending_install",
                publish_generation=0,
                security_generation=0,
                gc_generation=0,
                created_at=now,
            )
        )
        session.flush()
        session.add(
            StoredBlobInspectionRow(
                workspace_id=workspace_id,
                id=inspection_id,
                blob_id=blob_id,
                blob_epoch=1,
                registry_version="registry-1",
                detector_id="detector",
                detector_version="1",
                format_id="txt",
                canonical_media_type="text/plain",
                validation_schema_version="1",
                metadata_json={},
                scanner_verdict="not_configured",
                ordinary_available=True,
                created_at=now,
            )
        )
        session.commit()

        with pytest.raises(IntegrityError, match="media evidence is immutable"):
            session.execute(
                text(
                    "UPDATE stored_blob_inspection SET format_id = 'md' "
                    "WHERE workspace_id = :workspace_id AND id = :id"
                ),
                {"workspace_id": workspace_id.hex, "id": inspection_id.hex},
            )
            session.commit()
        session.rollback()

        with pytest.raises(
            IntegrityError,
            match="media evidence delete requires barrier",
        ):
            session.execute(
                text(
                    "DELETE FROM stored_blob_inspection "
                    "WHERE workspace_id = :workspace_id AND id = :id"
                ),
                {"workspace_id": workspace_id.hex, "id": inspection_id.hex},
            )
            session.commit()
        session.rollback()

        intent = session.get(WorkspaceMediaWriteIntent, workspace_id)
        assert intent is not None
        intent.state = "deleting"
        intent.delete_generation += 1
        session.commit()
        session.execute(
            text(
                "DELETE FROM stored_blob_inspection "
                "WHERE workspace_id = :workspace_id AND id = :id"
            ),
            {"workspace_id": workspace_id.hex, "id": inspection_id.hex},
        )
        session.commit()


def test_repository_preserves_epoch_seed_across_purge_and_reupload(
    media_engine: Engine,
) -> None:
    factory = sessionmaker(media_engine, expire_on_commit=False)
    uow_factory = SqlAlchemyMediaUnitOfWorkFactory(factory)
    with factory() as session:
        workspace_id = _create_workspace(session, "aba")
        _configure_media(session, workspace_id)
    blob_id = uuid4()
    identity = WorkspaceBlobIdentity(workspace_id, Sha256Digest("e" * 64))
    aggregate = StoredBlob(blob_id=blob_id, identity=identity).allocate(
        byte_size=5,
        storage_key=f"workspaces/{workspace_id}/{blob_id}/1/object",
    )
    with uow_factory() as uow:
        uow.repository.put(aggregate)
        uow.commit()

    with uow_factory() as uow:
        current = uow.repository.get_by_blob_id(workspace_id, blob_id)
        assert current is not None and current.active is not None
        available = current.update_active(
            current.active.transition(BlobState.AVAILABLE, generation=1)
        )
        uow.repository.put(available)
        uow.commit()
    with uow_factory() as uow:
        current = uow.repository.get_by_blob_id(workspace_id, blob_id)
        assert current is not None and current.active is not None
        marked = current.update_active(
            current.active.transition(BlobState.GC_PENDING, generation=1)
        )
        uow.repository.put(marked)
        uow.commit()
    with uow_factory() as uow:
        current = uow.repository.get_by_blob_id(workspace_id, blob_id)
        assert current is not None and current.active is not None
        trashed = current.update_active(
            current.active.transition(BlobState.TRASHED, generation=1)
        )
        uow.repository.put(trashed)
        uow.commit()
    with uow_factory() as uow:
        current = uow.repository.get_by_blob_id(workspace_id, blob_id)
        assert current is not None and current.active is not None
        purged = current.update_active(
            current.active.transition(BlobState.PURGED, generation=1)
        )
        uow.repository.put(purged)
        uow.commit()
    with uow_factory() as uow:
        current = uow.repository.get_by_blob_id(workspace_id, blob_id)
        assert current is not None
        assert current.active is None
        assert current.next_epoch.value == 2
        replacement = current.allocate(
            byte_size=5,
            storage_key=f"workspaces/{workspace_id}/{blob_id}/2/object",
        )
        uow.repository.put(replacement)
        uow.commit()

    with factory() as session:
        epochs = session.scalars(
            select(BlobIncarnationRow)
            .where(
                BlobIncarnationRow.workspace_id == workspace_id,
                BlobIncarnationRow.blob_id == blob_id,
            )
            .order_by(BlobIncarnationRow.blob_epoch)
        ).all()
        assert [(row.blob_epoch, row.state) for row in epochs] == [
            (1, "purged"),
            (2, "pending_install"),
        ]
        pointer = session.get(
            StoredBlobActiveIncarnationRow,
            {"workspace_id": workspace_id, "blob_id": blob_id},
        )
        assert pointer is not None and pointer.blob_epoch == 2


def test_quota_release_is_atomic_and_replay_refunds_once(
    media_engine: Engine,
) -> None:
    factory = sessionmaker(media_engine, expire_on_commit=False)
    uow_factory = SqlAlchemyMediaUnitOfWorkFactory(factory)
    with factory() as session:
        workspace_id = _create_workspace(session, "quota")
        _configure_media(session, workspace_id, limit_bytes=10)
        staging_id, key = _create_staging_lease(
            session,
            workspace_id,
            byte_size=10,
        )
    charge_id = uuid4()
    target = PhysicalChargeTarget(
        workspace_id=workspace_id,
        kind=PhysicalObjectKind.STAGING_LEASE,
        object_id=staging_id,
        generation=1,
    )
    with uow_factory() as uow:
        uow.physical_quota.reserve(
            workspace_id,
            charge_id=charge_id,
            target=target,
            storage_key=key,
            bytes_=10,
        )
        uow.commit()
    with uow_factory() as uow:
        assert uow.physical_quota.charged_bytes(workspace_id) == 10
        receipt = uow.physical_quota.release_after_deletion(
            workspace_id,
            charge_id=charge_id,
            deletion=PhysicalDeleteResult(
                PhysicalDeleteDisposition.DELETED,
                target,
                key,
            ),
        )
        uow.commit()
    with uow_factory() as uow:
        replay = uow.physical_quota.release_after_deletion(
            workspace_id,
            charge_id=charge_id,
            deletion=PhysicalDeleteResult(
                PhysicalDeleteDisposition.ALREADY_MISSING,
                target,
                key,
            ),
        )
        assert replay == receipt
        assert uow.physical_quota.charged_bytes(workspace_id) == 0
        uow.commit()

    with factory() as session:
        charge = session.get(
            MediaPhysicalCharge,
            {"workspace_id": workspace_id, "id": charge_id},
        )
        receipts = session.scalars(
            select(MediaDeletionReceipt).where(
                MediaDeletionReceipt.workspace_id == workspace_id,
                MediaDeletionReceipt.physical_charge_id == charge_id,
            )
        ).all()
        usage = session.get(WorkspaceMediaQuotaUsage, workspace_id)
        assert charge is not None and charge.state == "released"
        assert len(receipts) == 1
        assert usage is not None and usage.charged_bytes == 0


def test_quota_and_outbox_roll_back_with_the_unit_of_work(
    media_engine: Engine,
) -> None:
    factory = sessionmaker(media_engine, expire_on_commit=False)
    uow_factory = SqlAlchemyMediaUnitOfWorkFactory(factory)
    with factory() as session:
        workspace_id = _create_workspace(session, "rollback")
        _configure_media(session, workspace_id, limit_bytes=9)
        staging_id, key = _create_staging_lease(
            session,
            workspace_id,
            byte_size=9,
        )
    target = PhysicalChargeTarget(
        workspace_id=workspace_id,
        kind=PhysicalObjectKind.STAGING_LEASE,
        object_id=staging_id,
        generation=1,
    )
    with uow_factory() as uow:
        uow.physical_quota.reserve(
            workspace_id,
            charge_id=uuid4(),
            target=target,
            storage_key=key,
            bytes_=9,
        )
        uow.outbox.add(
            OutboxMessage(
                workspace_id,
                "stage:rollback",
                "InstallRequested",
                "1",
                (("staging_id", str(staging_id)),),
            )
        )

    with factory() as session:
        usage = session.get(WorkspaceMediaQuotaUsage, workspace_id)
        assert usage is not None and usage.charged_bytes == 0
        assert session.scalar(
            select(MediaPhysicalCharge.id).where(
                MediaPhysicalCharge.workspace_id == workspace_id
            )
        ) is None
        assert session.scalar(
            text("SELECT COUNT(*) FROM media_outbox WHERE workspace_id = :workspace"),
            {"workspace": workspace_id.hex},
        ) == 0

        oversized_staging_id, oversized_key = _create_staging_lease(
            session,
            workspace_id,
            byte_size=10,
        )
    oversized_target = PhysicalChargeTarget(
        workspace_id=workspace_id,
        kind=PhysicalObjectKind.STAGING_LEASE,
        object_id=oversized_staging_id,
        generation=1,
    )

    with uow_factory() as uow:
        with pytest.raises(ValueError, match="media_quota_exceeded"):
            uow.physical_quota.reserve(
                workspace_id,
                charge_id=uuid4(),
                target=oversized_target,
                storage_key=oversized_key,
                bytes_=10,
            )


def test_outbox_idempotency_is_workspace_and_topic_scoped(
    media_engine: Engine,
) -> None:
    factory = sessionmaker(media_engine, expire_on_commit=False)
    uow_factory = SqlAlchemyMediaUnitOfWorkFactory(factory)
    with factory() as session:
        left = _create_workspace(session, "outbox-left")
        right = _create_workspace(session, "outbox-right")
        _configure_media(session, left)
        _configure_media(session, right)
    for workspace_id in (left, right):
        with uow_factory() as uow:
            uow.outbox.add(
                OutboxMessage(
                    workspace_id,
                    "same-key",
                    "InstallRequested",
                    "1",
                    (("value", "same"),),
                )
            )
            uow.commit()

    with uow_factory() as uow:
        with pytest.raises(IdempotencyConflictError, match="outbox"):
            uow.outbox.add(
                OutboxMessage(
                    left,
                    "same-key",
                    "InstallRequested",
                    "1",
                    (("value", "different"),),
                )
            )

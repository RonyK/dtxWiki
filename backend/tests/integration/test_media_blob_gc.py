from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from backend.app.container import BlobStoreProvider, DatabaseProvider, ServiceRegistry
from backend.app.db import make_engine, make_session_factory
from backend.app.settings import Settings
from backend.media.adapters.sqlalchemy import (
    SqlAlchemyBlobGcService,
    SqlAlchemyMediaInstallJournal,
)
from backend.media.application import InstallValidatedBlob
from backend.media.domain import BlobReference
from backend.media.ports.contracts import (
    GenerationFence,
    PhysicalChargeTarget,
    PhysicalDeleteDisposition,
    PhysicalObjectKind,
    StorageKey,
)
from backend.media.ports.install import (
    BlobAssociationHold,
    BlobAssociationHoldProducerKind,
)
from backend.models.base import Base
from backend.models.library import (
    LibraryArtifact,
    LibraryArtifactConversionDetail,
    LibraryArtifactPreviewDetail,
    LibraryAttempt,
    LibraryAttemptPreviewDetail,
    LibraryItem,
    LibraryItemVersion,
    LibraryRun,
    LibraryRunConversionDetail,
    LibraryRunPreviewDetail,
)
from backend.models.media import (
    BlobIncarnationRow,
    MediaBlobGcCandidate,
    MediaBlobGcHold,
    MediaBlobReference,
    MediaDeletionReceipt,
    MediaPhysicalCharge,
    StoredBlobActiveIncarnationRow,
    WorkspaceMediaQuotaUsage,
)
from backend.repositories.media_blob_reference import (
    BlobReferenceOwnerKind,
    BlobReferenceUnavailableError,
    SqlAlchemyBlobReferenceLedger,
)
from backend.scripts.seed_dev import seed_default_workspace
from backend.services.job_queue import JobQueueService
from backend.services.library_blob_storage import FilesystemLibraryBlobStore
from backend.services.library_service import LibraryService
from backend.services.media_blob_gc_holds import media_blob_gc_hold_is_active
from backend.worker.runtime import OneShotWorker

pytestmark = pytest.mark.integration


@dataclass(frozen=True, slots=True)
class _InstalledFixture:
    database_url: str
    factory: sessionmaker
    store: FilesystemLibraryBlobStore
    workspace_id: UUID
    blob: BlobReference
    storage_key: StorageKey
    content: bytes


@pytest.fixture
def installed_blob(tmp_path: Path):
    database_path = tmp_path / "media-blob-gc.sqlite"
    database_url = f"sqlite+pysqlite:///{database_path.as_posix()}"
    engine = make_engine(database_url)
    Base.metadata.create_all(engine)
    factory = make_session_factory(engine)
    with factory.begin() as session:
        workspace = seed_default_workspace(session)
        workspace_id = workspace.id
        session.add(
            WorkspaceMediaQuotaUsage(
                workspace_id=workspace_id,
                limit_bytes=1024 * 1024,
                charged_bytes=0,
                revision=0,
                updated_at=datetime.now(UTC),
            )
        )

    store = FilesystemLibraryBlobStore(tmp_path / "blob-store")
    installer = InstallValidatedBlob(
        stager=store,
        publisher=store,
        deleter=store,
        layout=store,
        journal=SqlAlchemyMediaInstallJournal(factory),
    )
    content = b"cross-domain blob reference ledger"
    available = installer.execute(
        workspace_id=workspace_id,
        actor_id=uuid4(),
        session_key="media-blob-gc:test-session",
        idempotency_key="media-blob-gc:test-install",
        content=content,
        max_bytes=len(content),
        canonical_media_type="image/png",
        format_id="png",
        detector_id="test-detector",
        detector_version="1",
        registry_version="test-registry-v1",
        validation_schema_version="test-schema-v1",
        metadata={},
    )
    fixture = _InstalledFixture(
        database_url=database_url,
        factory=factory,
        store=store,
        workspace_id=workspace_id,
        blob=available.reference,
        storage_key=StorageKey(available.storage_key),
        content=content,
    )
    try:
        yield fixture
    finally:
        engine.dispose()


def _gc_service(fixture: _InstalledFixture) -> SqlAlchemyBlobGcService:
    return SqlAlchemyBlobGcService(
        session_for=lambda _workspace_id: fixture.factory,
        store_for=lambda _workspace_id: fixture.store,
        hold_is_active=media_blob_gc_hold_is_active,
    )


def _register_then_remove_last_owner(fixture: _InstalledFixture) -> None:
    owners = tuple((owner_kind, uuid4()) for owner_kind in BlobReferenceOwnerKind)
    with fixture.factory.begin() as session:
        ledger = SqlAlchemyBlobReferenceLedger(session)
        for owner_kind, owner_id in owners:
            assert ledger.register(
                owner_kind=owner_kind,
                owner_id=owner_id,
                blob=fixture.blob,
            )

    for owner_kind, owner_id in owners[:-1]:
        with fixture.factory.begin() as session:
            assert SqlAlchemyBlobReferenceLedger(session).unregister(
                owner_kind=owner_kind,
                owner_id=owner_id,
                blob=fixture.blob,
            )
            assert session.get(
                MediaBlobGcCandidate,
                {
                    "workspace_id": fixture.workspace_id,
                    "blob_id": fixture.blob.blob_id,
                    "blob_epoch": fixture.blob.epoch.value,
                },
            ) is None

    owner_kind, owner_id = owners[-1]
    with fixture.factory.begin() as session:
        assert SqlAlchemyBlobReferenceLedger(session).unregister(
            owner_kind=owner_kind,
            owner_id=owner_id,
            blob=fixture.blob,
        )
        assert session.get(
            MediaBlobGcCandidate,
            {
                "workspace_id": fixture.workspace_id,
                "blob_id": fixture.blob.blob_id,
                "blob_epoch": fixture.blob.epoch.value,
            },
        ) is not None


def test_available_install_is_collectable_until_first_owner_commits(
    installed_blob: _InstalledFixture,
) -> None:
    owner_id = uuid4()
    with installed_blob.factory.begin() as session:
        candidate = session.get(
            MediaBlobGcCandidate,
            {
                "workspace_id": installed_blob.workspace_id,
                "blob_id": installed_blob.blob.blob_id,
                "blob_epoch": installed_blob.blob.epoch.value,
            },
        )
        assert candidate is not None
        assert candidate.reason == "unowned_install"
        assert candidate.eligible_at == candidate.requested_at
        assert SqlAlchemyBlobReferenceLedger(session).register(
            owner_kind=BlobReferenceOwnerKind.CONTEXT_MEDIA_ASSET,
            owner_id=owner_id,
            blob=installed_blob.blob,
        )
        assert session.get(
            MediaBlobGcCandidate,
            {
                "workspace_id": installed_blob.workspace_id,
                "blob_id": installed_blob.blob.blob_id,
                "blob_epoch": installed_blob.blob.epoch.value,
            },
        ) is None
        assert session.scalar(
            select(func.count())
            .select_from(MediaBlobGcHold)
            .where(
                MediaBlobGcHold.workspace_id == installed_blob.workspace_id,
                MediaBlobGcHold.blob_id == installed_blob.blob.blob_id,
                MediaBlobGcHold.blob_epoch == installed_blob.blob.epoch.value,
            )
        ) == 0


def test_expired_context_hold_loses_to_gc_and_late_association_fails_closed(
    installed_blob: _InstalledFixture,
) -> None:
    now = datetime.now(UTC)
    owner_id = uuid4()
    actor_id = uuid4()
    installer = InstallValidatedBlob(
        stager=installed_blob.store,
        publisher=installed_blob.store,
        deleter=installed_blob.store,
        layout=installed_blob.store,
        journal=SqlAlchemyMediaInstallJournal(installed_blob.factory),
    )
    available = installer.execute(
        workspace_id=installed_blob.workspace_id,
        actor_id=actor_id,
        session_key="context:expired-association-hold",
        idempotency_key=f"context:{owner_id}",
        content=installed_blob.content,
        max_bytes=len(installed_blob.content),
        canonical_media_type="image/png",
        format_id="png",
        detector_id="test-detector",
        detector_version="1",
        registry_version="test-registry-v1",
        validation_schema_version="test-schema-v1",
        association_hold=BlobAssociationHold(
            producer_kind=BlobAssociationHoldProducerKind.CONTEXT_MEDIA_UPLOAD,
            producer_id=owner_id,
            producer_generation=1,
            expires_at=now + timedelta(minutes=1),
        ),
    )

    collected = SqlAlchemyBlobGcService(
        session_for=lambda _workspace_id: installed_blob.factory,
        store_for=lambda _workspace_id: installed_blob.store,
        hold_is_active=media_blob_gc_hold_is_active,
        clock=lambda: now + timedelta(minutes=2),
    ).sweep(workspace_id=installed_blob.workspace_id)
    assert collected.purged_count == 1

    # Safety does not depend on predicting request duration. Once GC wins the
    # incarnation CAS, the late domain UoW cannot attach a reference to missing
    # bytes, so this domain UoW rolls back.
    with pytest.raises(BlobReferenceUnavailableError, match="not available"):
        with installed_blob.factory.begin() as session:
            SqlAlchemyBlobReferenceLedger(session).register(
                owner_kind=BlobReferenceOwnerKind.CONTEXT_MEDIA_ASSET,
                owner_id=owner_id,
                blob=available.reference,
            )

    # An exact caller replay is then rebound to root.next_epoch. It never
    # revives the purged incarnation or reuses its physical storage fence.
    recovered = installer.execute(
        workspace_id=installed_blob.workspace_id,
        actor_id=actor_id,
        session_key="context:expired-association-hold",
        idempotency_key=f"context:{owner_id}",
        content=installed_blob.content,
        max_bytes=len(installed_blob.content),
        canonical_media_type="image/png",
        format_id="png",
        detector_id="test-detector",
        detector_version="1",
        registry_version="test-registry-v1",
        validation_schema_version="test-schema-v1",
        association_hold=BlobAssociationHold(
            producer_kind=BlobAssociationHoldProducerKind.CONTEXT_MEDIA_UPLOAD,
            producer_id=owner_id,
            producer_generation=1,
            expires_at=now + timedelta(hours=1),
        ),
    )
    assert recovered.reference.blob_id == available.reference.blob_id
    assert recovered.reference.epoch.value == available.reference.epoch.value + 1
    with installed_blob.factory.begin() as session:
        assert SqlAlchemyBlobReferenceLedger(session).register(
            owner_kind=BlobReferenceOwnerKind.CONTEXT_MEDIA_ASSET,
            owner_id=owner_id,
            blob=recovered.reference,
        )


def test_unowned_install_without_an_association_hold_is_immediately_purgeable(
    installed_blob: _InstalledFixture,
) -> None:
    with installed_blob.factory() as session:
        candidate = session.get(
            MediaBlobGcCandidate,
            {
                "workspace_id": installed_blob.workspace_id,
                "blob_id": installed_blob.blob.blob_id,
                "blob_epoch": installed_blob.blob.epoch.value,
            },
        )
        assert candidate is not None
        eligible_at = candidate.eligible_at

    result = SqlAlchemyBlobGcService(
        session_for=lambda _workspace_id: installed_blob.factory,
        store_for=lambda _workspace_id: installed_blob.store,
        hold_is_active=media_blob_gc_hold_is_active,
        clock=lambda: eligible_at,
    ).sweep(workspace_id=installed_blob.workspace_id)
    assert result.purged_count == 1


def test_purged_digest_reinstalls_as_a_new_fenced_incarnation(
    installed_blob: _InstalledFixture,
) -> None:
    first = installed_blob.blob
    assert _gc_service(installed_blob).sweep(
        workspace_id=installed_blob.workspace_id
    ).purged_count == 1

    installer = InstallValidatedBlob(
        stager=installed_blob.store,
        publisher=installed_blob.store,
        deleter=installed_blob.store,
        layout=installed_blob.store,
        journal=SqlAlchemyMediaInstallJournal(installed_blob.factory),
    )
    reinstalled = installer.execute(
        workspace_id=installed_blob.workspace_id,
        actor_id=uuid4(),
        session_key="media-blob-gc:test-session",
        # This is the same logical operation key as the collected install.
        # The application binds it to the DB-authoritative next incarnation
        # without reusing epoch-one storage or its physical charge identity.
        idempotency_key="media-blob-gc:test-install",
        content=installed_blob.content,
        max_bytes=len(installed_blob.content),
        canonical_media_type="image/png",
        format_id="png",
        detector_id="test-detector",
        detector_version="1",
        registry_version="test-registry-v1",
        validation_schema_version="test-schema-v1",
        metadata={},
    )

    assert reinstalled.reference.blob_id == first.blob_id
    assert reinstalled.reference.epoch.value == first.epoch.value + 1
    assert reinstalled.storage_key != installed_blob.storage_key.value
    assert installed_blob.store.open(StorageKey(reinstalled.storage_key)).read() == (
        installed_blob.content
    )
    with installed_blob.factory() as session:
        old_incarnation = session.get(
            BlobIncarnationRow,
            {
                "workspace_id": installed_blob.workspace_id,
                "blob_id": first.blob_id,
                "blob_epoch": first.epoch.value,
            },
        )
        new_incarnation = session.get(
            BlobIncarnationRow,
            {
                "workspace_id": installed_blob.workspace_id,
                "blob_id": reinstalled.reference.blob_id,
                "blob_epoch": reinstalled.reference.epoch.value,
            },
        )
        assert old_incarnation is not None and old_incarnation.state == "purged"
        assert new_incarnation is not None and new_incarnation.state == "available"
        assert session.scalar(
            select(func.count())
            .select_from(MediaPhysicalCharge)
            .where(
                MediaPhysicalCharge.workspace_id == installed_blob.workspace_id,
                MediaPhysicalCharge.target_kind
                == PhysicalObjectKind.BLOB_INCARNATION.value,
                MediaPhysicalCharge.state == "charged",
            )
        ) == 1


def test_context_association_hold_fences_gc_until_owner_registration(
    installed_blob: _InstalledFixture,
) -> None:
    now = datetime.now(UTC)
    owner_id = uuid4()
    # Reuse is the dangerous path too: another owner may remove the last
    # durable edge after lookup but before this caller persists its own. The
    # journal must install the hold under the incarnation lock even when no new
    # bytes are published.
    content = installed_blob.content
    installer = InstallValidatedBlob(
        stager=installed_blob.store,
        publisher=installed_blob.store,
        deleter=installed_blob.store,
        layout=installed_blob.store,
        journal=SqlAlchemyMediaInstallJournal(installed_blob.factory),
    )
    available = installer.execute(
        workspace_id=installed_blob.workspace_id,
        actor_id=uuid4(),
        session_key="context:association-hold",
        idempotency_key=f"context:{owner_id}",
        content=content,
        max_bytes=len(content),
        canonical_media_type="image/png",
        format_id="png",
        detector_id="test-detector",
        detector_version="1",
        registry_version="test-registry-v1",
        validation_schema_version="test-schema-v1",
        association_hold=BlobAssociationHold(
            producer_kind=BlobAssociationHoldProducerKind.CONTEXT_MEDIA_UPLOAD,
            producer_id=owner_id,
            producer_generation=1,
            expires_at=now + timedelta(hours=1),
        ),
    )

    fenced = SqlAlchemyBlobGcService(
        session_for=lambda _workspace_id: installed_blob.factory,
        store_for=lambda _workspace_id: installed_blob.store,
        hold_is_active=media_blob_gc_hold_is_active,
        clock=lambda: now + timedelta(minutes=30),
    ).sweep(workspace_id=installed_blob.workspace_id)
    assert fenced.inspected_count == 1
    assert fenced.purged_count == 0

    with installed_blob.factory.begin() as session:
        assert SqlAlchemyBlobReferenceLedger(session).register(
            owner_kind=BlobReferenceOwnerKind.CONTEXT_MEDIA_ASSET,
            owner_id=owner_id,
            blob=available.reference,
        )
        assert session.get(
            MediaBlobGcCandidate,
            {
                "workspace_id": installed_blob.workspace_id,
                "blob_id": available.reference.blob_id,
                "blob_epoch": available.reference.epoch.value,
            },
        ) is None
        assert session.scalar(
            select(func.count())
            .select_from(MediaBlobGcHold)
            .where(
                MediaBlobGcHold.workspace_id == installed_blob.workspace_id,
                MediaBlobGcHold.blob_id == available.reference.blob_id,
                MediaBlobGcHold.blob_epoch == available.reference.epoch.value,
            )
        ) == 0


def test_running_conversion_generation_fences_gc_without_a_time_guess(
    installed_blob: _InstalledFixture,
) -> None:
    with installed_blob.factory.begin() as session:
        item = LibraryItem(
            workspace_id=installed_blob.workspace_id,
            connector_type="upload",
            format_hint="png",
            uri=f"upload://active-hold-{uuid4()}.png",
            title="Active conversion hold",
            trust_level="trusted",
            lifecycle_status="processing",
        )
        session.add(item)
        session.flush()
        version = LibraryItemVersion(
            workspace_id=installed_blob.workspace_id,
            library_item_id=item.id,
            version_no=1,
            content_hash="e" * 64,
            raw_storage_ref="legacy://active-hold-source",
            original_filename="source.png",
            detected_media_type="image/png",
            format_id="png",
            byte_size=1,
            detection_json={"detector_id": "test"},
            processing_status="converting",
        )
        session.add(version)
        session.flush()
        run = LibraryRun(
            workspace_id=installed_blob.workspace_id,
            library_item_version_id=version.id,
            stage="conversion",
            run_seq=1,
            reason="initial",
            status="running",
            provider_chain_json=[],
            policy_revision="test",
            output_schema_version="1",
            idempotency_key=f"active-hold:{version.id}",
        )
        run.conversion_detail = LibraryRunConversionDetail(
            error_fallback_matrix_json={},
            per_provider_budget_json={},
            total_budget_json={},
        )
        session.add(run)
        session.flush()
        attempt = LibraryAttempt(
            workspace_id=installed_blob.workspace_id,
            run_id=run.id,
            library_item_version_id=version.id,
            stage="conversion",
            provider_id="test.converter",
            provider_version="1",
            provider_spi_version="1",
            config_hash="f" * 64,
            input_sha256="e" * 64,
            output_schema_version="1",
            status="running",
            chain_index=0,
            transport_retry_count=0,
            execution_generation=7,
        )
        session.add(attempt)
        session.flush()
        session.add(
            MediaBlobGcHold(
                workspace_id=installed_blob.workspace_id,
                producer_kind=(
                    BlobAssociationHoldProducerKind.LIBRARY_CONVERSION_ATTEMPT.value
                ),
                producer_id=attempt.id,
                producer_generation=7,
                blob_id=installed_blob.blob.blob_id,
                blob_epoch=installed_blob.blob.epoch.value,
                expires_at=None,
            )
        )
        attempt_id = attempt.id

    far_future = datetime.now(UTC) + timedelta(days=365)
    active = SqlAlchemyBlobGcService(
        session_for=lambda _workspace_id: installed_blob.factory,
        store_for=lambda _workspace_id: installed_blob.store,
        hold_is_active=media_blob_gc_hold_is_active,
        clock=lambda: far_future,
    ).sweep(workspace_id=installed_blob.workspace_id)
    assert active.inspected_count == 1
    assert active.purged_count == 0

    with installed_blob.factory.begin() as session:
        attempt = session.get(LibraryAttempt, attempt_id)
        assert attempt is not None
        # A requeue advances the attempt generation. The old worker's hold is
        # no longer authoritative even though the new generation is running.
        attempt.execution_generation = 8

    terminal = SqlAlchemyBlobGcService(
        session_for=lambda _workspace_id: installed_blob.factory,
        store_for=lambda _workspace_id: installed_blob.store,
        hold_is_active=media_blob_gc_hold_is_active,
        clock=lambda: far_future,
    ).sweep(workspace_id=installed_blob.workspace_id)
    assert terminal.purged_count == 1
    with pytest.raises(BlobReferenceUnavailableError, match="not available"):
        with installed_blob.factory.begin() as session:
            SqlAlchemyBlobReferenceLedger(session).register(
                owner_kind=BlobReferenceOwnerKind.LIBRARY_ARTIFACT,
                owner_id=uuid4(),
                blob=installed_blob.blob,
            )


def test_shared_reference_ledger_queues_only_after_the_last_domain_owner(
    installed_blob: _InstalledFixture,
) -> None:
    _register_then_remove_last_owner(installed_blob)

    with installed_blob.factory() as session:
        assert session.scalar(select(func.count()).select_from(MediaBlobReference)) == 0
        assert (
            session.scalar(select(func.count()).select_from(MediaBlobGcCandidate)) == 1
        )


class _FailFirstFinalize(SqlAlchemyBlobGcService):
    def __init__(self, fixture: _InstalledFixture) -> None:
        super().__init__(
            session_for=lambda _workspace_id: fixture.factory,
            store_for=lambda _workspace_id: fixture.store,
            hold_is_active=media_blob_gc_hold_is_active,
        )
        self.failed = False

    def _finalize_purged(self, work, deletion) -> str:
        if not self.failed:
            self.failed = True
            raise RuntimeError("simulated crash after physical purge")
        return super()._finalize_purged(work, deletion)


def test_purge_finalize_crash_stays_fail_closed_and_replay_releases_exact_quota(
    installed_blob: _InstalledFixture,
) -> None:
    _register_then_remove_last_owner(installed_blob)
    faulting = _FailFirstFinalize(installed_blob)

    with pytest.raises(RuntimeError, match="simulated crash after physical purge"):
        faulting.sweep(workspace_id=installed_blob.workspace_id)

    with installed_blob.factory() as session:
        incarnation = session.get(
            BlobIncarnationRow,
            {
                "workspace_id": installed_blob.workspace_id,
                "blob_id": installed_blob.blob.blob_id,
                "blob_epoch": installed_blob.blob.epoch.value,
            },
        )
        charge = session.scalar(
            select(MediaPhysicalCharge).where(
                MediaPhysicalCharge.workspace_id == installed_blob.workspace_id,
                MediaPhysicalCharge.target_kind
                == PhysicalObjectKind.BLOB_INCARNATION.value,
            )
        )
        quota = session.get(WorkspaceMediaQuotaUsage, installed_blob.workspace_id)
        assert incarnation is not None and incarnation.state == "trashed"
        assert incarnation.gc_generation == 1
        assert charge is not None and charge.state == "charged"
        assert quota is not None and quota.charged_bytes == len(installed_blob.content)
        assert (
            session.scalar(select(func.count()).select_from(MediaDeletionReceipt)) == 1
        )
        # The install's staging deletion receipt exists; physical blob release
        # has not been recorded yet.
        assert session.scalar(
            select(func.count())
            .select_from(MediaDeletionReceipt)
            .where(MediaDeletionReceipt.physical_charge_id == charge.id)
        ) == 0

    with pytest.raises(BlobReferenceUnavailableError, match="not available"):
        with installed_blob.factory.begin() as session:
            SqlAlchemyBlobReferenceLedger(session).register(
                owner_kind=BlobReferenceOwnerKind.CONTEXT_MEDIA_ASSET,
                owner_id=uuid4(),
                blob=installed_blob.blob,
            )

    replay = _gc_service(installed_blob).sweep(
        workspace_id=installed_blob.workspace_id
    )
    assert replay.purged_count == 1

    with installed_blob.factory() as session:
        incarnation = session.get(
            BlobIncarnationRow,
            {
                "workspace_id": installed_blob.workspace_id,
                "blob_id": installed_blob.blob.blob_id,
                "blob_epoch": installed_blob.blob.epoch.value,
            },
        )
        charge = session.scalar(
            select(MediaPhysicalCharge).where(
                MediaPhysicalCharge.workspace_id == installed_blob.workspace_id,
                MediaPhysicalCharge.target_kind
                == PhysicalObjectKind.BLOB_INCARNATION.value,
            )
        )
        receipt = session.scalar(
            select(MediaDeletionReceipt).where(
                MediaDeletionReceipt.physical_charge_id == charge.id
            )
        )
        quota = session.get(WorkspaceMediaQuotaUsage, installed_blob.workspace_id)
        assert incarnation is not None and incarnation.state == "purged"
        assert charge is not None and charge.state == "released"
        assert receipt is not None and receipt.disposition == "already_missing"
        assert quota is not None and quota.charged_bytes == 0
        assert session.get(
            StoredBlobActiveIncarnationRow,
            {
                "workspace_id": installed_blob.workspace_id,
                "blob_id": installed_blob.blob.blob_id,
            },
        ) is None
        assert (
            session.scalar(select(func.count()).select_from(MediaBlobGcCandidate)) == 0
        )


def test_filesystem_gc_trash_isolated_by_gc_generation(
    installed_blob: _InstalledFixture,
) -> None:
    with installed_blob.factory() as session:
        charge = session.scalar(
            select(MediaPhysicalCharge).where(
                MediaPhysicalCharge.workspace_id == installed_blob.workspace_id,
                MediaPhysicalCharge.target_kind
                == PhysicalObjectKind.BLOB_INCARNATION.value,
            )
        )
    assert charge is not None
    target = PhysicalChargeTarget(
        workspace_id=installed_blob.workspace_id,
        kind=PhysicalObjectKind.BLOB_INCARNATION,
        object_id=installed_blob.blob.blob_id,
        generation=charge.target_generation,
        blob=installed_blob.blob,
    )
    first = GenerationFence(blob=installed_blob.blob, generation=1)
    second = GenerationFence(blob=installed_blob.blob, generation=2)

    installed_blob.store.trash(installed_blob.storage_key, fence=first)
    installed_blob.store.restore(installed_blob.storage_key, fence=first)
    installed_blob.store.trash(installed_blob.storage_key, fence=second)

    stale = installed_blob.store.purge(
        installed_blob.storage_key,
        fence=first,
        target=target,
    )
    assert stale.disposition is PhysicalDeleteDisposition.ALREADY_MISSING
    installed_blob.store.restore(installed_blob.storage_key, fence=second)
    handle = installed_blob.store.open(installed_blob.storage_key)
    try:
        assert handle.read() == installed_blob.content
    finally:
        handle.close()


class _RecordingBlobStoreProvider(BlobStoreProvider):
    def __init__(self, store: FilesystemLibraryBlobStore) -> None:
        super().__init__(None, store=store)
        self.workspace_ids: list[UUID] = []

    def store_for(self, workspace_id: UUID | None):
        assert isinstance(workspace_id, UUID)
        self.workspace_ids.append(workspace_id)
        return super().store_for(workspace_id)


def test_worker_media_blob_gc_uses_routed_provider_after_candidate_commit(
    installed_blob: _InstalledFixture,
) -> None:
    _register_then_remove_last_owner(installed_blob)
    queue = JobQueueService()
    with installed_blob.factory.begin() as session:
        queue.enqueue(
            session=session,
            workspace_id=installed_blob.workspace_id,
            job_type="media_blob_gc",
            trigger_type="scheduled",
            idempotency_key="media-blob-gc:worker-test",
            target_ref={"scheduled_for": datetime.now(UTC).isoformat()},
        )

    settings = Settings(
        database_url=installed_blob.database_url,
        library_blob_root=str(installed_blob.store.root),
    )
    database_provider = DatabaseProvider(
        settings,
        session_factory=installed_blob.factory,
    )
    blob_provider = _RecordingBlobStoreProvider(installed_blob.store)
    services = ServiceRegistry()
    services.settings = settings
    worker = OneShotWorker(
        job_queue=queue,
        settings=settings,
        database_provider=database_provider,
        blob_store_provider=blob_provider,
        services=services,
    )

    result = worker.dispatch_once_with_session_factory(
        installed_blob.factory,
        workspace_id=installed_blob.workspace_id,
        job_types={"media_blob_gc"},
    )

    assert result is not None and result.status == "completed"
    assert result.detail == {
        "inspected_count": 1,
        "purged_count": 1,
        "cancelled_count": 0,
        "repaired_count": 0,
    }
    assert services.blob_store_provider is blob_provider
    assert blob_provider.workspace_ids == [installed_blob.workspace_id]


@pytest.mark.filterwarnings("error::sqlalchemy.exc.SAWarning")
def test_library_hard_delete_unregisters_source_conversion_and_preview_owners(
    installed_blob: _InstalledFixture,
) -> None:
    context_owner_id = uuid4()
    with installed_blob.factory.begin() as session:
        item = LibraryItem(
            workspace_id=installed_blob.workspace_id,
            connector_type="upload",
            format_hint="png",
            uri=f"upload://blob-ledger-{uuid4()}.png",
            title="Blob ledger deletion",
            trust_level="trusted",
            lifecycle_status="ready",
        )
        session.add(item)
        session.flush()
        version = LibraryItemVersion(
            workspace_id=installed_blob.workspace_id,
            library_item_id=item.id,
            version_no=1,
            content_hash="a" * 64,
            raw_storage_ref=None,
            source_blob_workspace_id=installed_blob.workspace_id,
            source_blob_id=installed_blob.blob.blob_id,
            source_blob_epoch=installed_blob.blob.epoch.value,
            original_filename="source.png",
            detected_media_type="image/png",
            format_id="png",
            byte_size=len(installed_blob.content),
            detection_json={"detector_id": "test"},
            processing_status="ready",
        )
        session.add(version)
        session.flush()
        conversion_run = LibraryRun(
            workspace_id=installed_blob.workspace_id,
            library_item_version_id=version.id,
            stage="conversion",
            run_seq=1,
            reason="initial",
            status="completed",
            provider_chain_json=[],
            policy_revision="test",
            output_schema_version="1",
            idempotency_key=f"conversion:{version.id}",
        )
        conversion_run.conversion_detail = LibraryRunConversionDetail(
            error_fallback_matrix_json={},
            per_provider_budget_json={},
            total_budget_json={},
        )
        session.add(conversion_run)
        session.flush()
        conversion_attempt = LibraryAttempt(
            workspace_id=installed_blob.workspace_id,
            run_id=conversion_run.id,
            library_item_version_id=version.id,
            stage="conversion",
            provider_id="test.converter",
            provider_version="1",
            provider_spi_version="1",
            config_hash="b" * 64,
            input_sha256="a" * 64,
            output_schema_version="1",
            status="completed",
            chain_index=0,
            transport_retry_count=0,
            execution_generation=1,
        )
        session.add(conversion_attempt)
        session.flush()
        artifact = LibraryArtifact(
            workspace_id=installed_blob.workspace_id,
            library_item_version_id=version.id,
            attempt_id=conversion_attempt.id,
            stage="conversion",
            blob_workspace_id=installed_blob.workspace_id,
            blob_id=installed_blob.blob.blob_id,
            blob_epoch=installed_blob.blob.epoch.value,
            role="asset",
            kind="image",
            media_type="image/png",
            content_sha256="a" * 64,
            byte_size=len(installed_blob.content),
            ordinal=0,
        )
        artifact.conversion_detail = LibraryArtifactConversionDetail(
            is_complete=True,
            location_model={},
        )
        session.add(artifact)
        session.flush()
        preview_run = LibraryRun(
            workspace_id=installed_blob.workspace_id,
            library_item_version_id=version.id,
            stage="preview_generation",
            run_seq=2,
            reason="initial",
            status="completed",
            provider_chain_json=[],
            policy_revision="test",
            output_schema_version="1",
            idempotency_key=f"preview:{version.id}",
        )
        preview_run.preview_detail = LibraryRunPreviewDetail(
            recipe_json={},
            limits_json={},
            input_dependency_json={"input_kind": "source"},
            renderer_contract_version="1",
        )
        session.add(preview_run)
        session.flush()
        preview_attempt = LibraryAttempt(
            workspace_id=installed_blob.workspace_id,
            run_id=preview_run.id,
            library_item_version_id=version.id,
            stage="preview_generation",
            provider_id="test.preview",
            provider_version="1",
            provider_spi_version="1",
            config_hash="d" * 64,
            input_sha256="a" * 64,
            output_schema_version="1",
            status="completed",
            chain_index=0,
            transport_retry_count=0,
            execution_generation=1,
        )
        preview_attempt.preview_detail = LibraryAttemptPreviewDetail(
            provider_distribution_digest="c" * 64,
            input_kind="source",
            input_attempt_id=None,
            input_artifact_id=None,
            renderer_contract_version="1",
        )
        session.add(preview_attempt)
        session.flush()
        preview_artifact = LibraryArtifact(
            workspace_id=installed_blob.workspace_id,
            library_item_version_id=version.id,
            attempt_id=preview_attempt.id,
            stage="preview_generation",
            blob_workspace_id=installed_blob.workspace_id,
            blob_id=installed_blob.blob.blob_id,
            blob_epoch=installed_blob.blob.epoch.value,
            role="preview",
            kind="image",
            media_type="image/png",
            content_sha256="a" * 64,
            byte_size=len(installed_blob.content),
            ordinal=0,
        )
        preview_artifact.preview_detail = LibraryArtifactPreviewDetail(
            renderer_contract="1",
            delivery="range",
            rank=0,
            metadata_json={},
        )
        session.add(preview_artifact)
        session.flush()

        item.current_version_id = version.id
        item.current_ready_version_id = version.id
        item.current_preview_version_id = version.id
        version.selected_conversion_attempt_id = conversion_attempt.id
        version.selected_preview_attempt_id = preview_attempt.id
        ledger = SqlAlchemyBlobReferenceLedger(session)
        for owner_kind, owner_id in (
            (BlobReferenceOwnerKind.LIBRARY_ITEM_VERSION, version.id),
            (BlobReferenceOwnerKind.LIBRARY_ARTIFACT, artifact.id),
            (
                BlobReferenceOwnerKind.LIBRARY_ARTIFACT,
                preview_artifact.id,
            ),
            (BlobReferenceOwnerKind.CONTEXT_MEDIA_ASSET, context_owner_id),
        ):
            ledger.register(
                owner_kind=owner_kind,
                owner_id=owner_id,
                blob=installed_blob.blob,
            )
        item_id = item.id

    with installed_blob.factory.begin() as session:
        LibraryService().delete_library_item(
            session=session,
            workspace_id=installed_blob.workspace_id,
            library_item_id=item_id,
            actor_kind="system",
            actor_id="blob-ledger-test",
        )

    with installed_blob.factory.begin() as session:
        remaining = session.scalars(select(MediaBlobReference)).all()
        assert [(row.owner_kind, row.owner_id) for row in remaining] == [
            (BlobReferenceOwnerKind.CONTEXT_MEDIA_ASSET.value, context_owner_id)
        ]
        assert (
            session.scalar(select(func.count()).select_from(MediaBlobGcCandidate)) == 0
        )
        assert SqlAlchemyBlobReferenceLedger(session).unregister(
            owner_kind=BlobReferenceOwnerKind.CONTEXT_MEDIA_ASSET,
            owner_id=context_owner_id,
            blob=installed_blob.blob,
        )
        assert (
            session.scalar(select(func.count()).select_from(MediaBlobGcCandidate)) == 1
        )


# --- M-8: GC against an in-flight owner registration on SQLite --------------


def _incarnation_key(fixture: _InstalledFixture) -> dict:
    return {
        "workspace_id": fixture.workspace_id,
        "blob_id": fixture.blob.blob_id,
        "blob_epoch": fixture.blob.epoch.value,
    }


def _trash_files(fixture: _InstalledFixture) -> list[Path]:
    return [path for path in fixture.store.trash_root.rglob("*") if path.is_file()]


def _assert_owned_and_readable(fixture: _InstalledFixture) -> None:
    with fixture.factory() as session:
        incarnation = session.get(BlobIncarnationRow, _incarnation_key(fixture))
        assert incarnation is not None and incarnation.state == "available"
        assert incarnation.trashed_at is None
        assert session.scalar(
            select(func.count())
            .select_from(MediaBlobReference)
            .where(MediaBlobReference.blob_id == fixture.blob.blob_id)
        ) == 1
        assert session.get(MediaBlobGcCandidate, _incarnation_key(fixture)) is None
    handle = fixture.store.open(fixture.storage_key)
    try:
        assert handle.read() == fixture.content
    finally:
        handle.close()
    assert _trash_files(fixture) == []


def test_gc_waits_for_an_in_flight_owner_registration_instead_of_trashing(
    installed_blob: _InstalledFixture, monkeypatch
) -> None:
    """The audit interleaving, made deterministic.

    A Library registration holds SQLite's write lock (``acquire_write_intent``)
    with its owner registered but not committed while a sweep runs. pysqlite
    used to run the sweep's decision reads in autocommit mode -- "no owner,
    candidate queued" -- and only then wait at its first UPDATE, committing
    GC_PENDING on stale evidence once the registration released the lock and
    trashing an owned blob. The registration below commits exactly when the
    sweep is about to take the write lock: before the fix that is after the
    stale reads, after it the sweep has read nothing yet.
    """

    import threading

    from sqlalchemy import event

    from backend.services.db_write_intent import acquire_write_intent

    gc_engine = make_engine(installed_blob.database_url)
    gc_factory = make_session_factory(gc_engine)
    gc_threads: set[int] = set()
    gc_about_to_write = threading.Event()

    writes = ("BEGIN", "UPDATE", "INSERT", "DELETE")

    @event.listens_for(gc_engine, "before_cursor_execute")
    def _watch(_conn, _cursor, statement, _params, _context, _executemany):
        if threading.get_ident() in gc_threads and (
            statement.lstrip().upper().startswith(writes)
        ):
            gc_about_to_write.set()

    trashed: list[StorageKey] = []
    real_trash = installed_blob.store.trash

    def recording_trash(key, *, fence):
        trashed.append(key)
        real_trash(key, fence=fence)

    monkeypatch.setattr(installed_blob.store, "trash", recording_trash)
    gc = SqlAlchemyBlobGcService(
        session_for=lambda _workspace_id: gc_factory,
        store_for=lambda _workspace_id: installed_blob.store,
        hold_is_active=media_blob_gc_hold_is_active,
    )
    outcome: dict = {}

    def run_gc() -> None:
        gc_threads.add(threading.get_ident())
        try:
            outcome["result"] = gc.sweep(workspace_id=installed_blob.workspace_id)
        except BaseException as exc:  # reported below, not swallowed
            outcome["error"] = exc

    sweeper = threading.Thread(target=run_gc)
    try:
        with installed_blob.factory.begin() as session:
            acquire_write_intent(session)
            SqlAlchemyBlobReferenceLedger(session).register(
                owner_kind=BlobReferenceOwnerKind.LIBRARY_ITEM_VERSION,
                owner_id=uuid4(),
                blob=installed_blob.blob,
            )
            session.flush()
            sweeper.start()
            assert gc_about_to_write.wait(timeout=30), "GC never tried to write"
        sweeper.join(timeout=60)
        assert not sweeper.is_alive()
    finally:
        gc_engine.dispose()

    assert "error" not in outcome, outcome
    # Not merely repaired afterwards: the sweep never decided to collect.
    assert trashed == []
    assert outcome["result"].purged_count == 0
    assert outcome["result"].repaired_count == 0
    _assert_owned_and_readable(installed_blob)


def _strand_owned_blob(fixture: _InstalledFixture, *, state: str) -> None:
    """What the race left behind: owned, claimed by GC, bytes in its trash."""

    with fixture.factory.begin() as session:
        assert SqlAlchemyBlobReferenceLedger(session).register(
            owner_kind=BlobReferenceOwnerKind.LIBRARY_ITEM_VERSION,
            owner_id=uuid4(),
            blob=fixture.blob,
        )
        incarnation = session.get(BlobIncarnationRow, _incarnation_key(fixture))
        assert incarnation is not None
        incarnation.gc_generation += 1
        incarnation.state = state
        if state == "trashed":
            incarnation.trashed_at = datetime.now(UTC)
        generation = incarnation.gc_generation
    fixture.store.trash(
        fixture.storage_key,
        fence=GenerationFence(blob=fixture.blob, generation=generation),
    )
    assert not fixture.store.path_for_key(fixture.storage_key.value).exists()
    assert _trash_files(fixture)


@pytest.mark.parametrize("state", ["gc_pending", "trashed"])
def test_sweep_restores_an_owned_blob_stranded_mid_collection(
    installed_blob: _InstalledFixture, state: str
) -> None:
    """No queue candidate is left to find it: registration deleted it."""

    _strand_owned_blob(installed_blob, state=state)

    result = _gc_service(installed_blob).sweep(
        workspace_id=installed_blob.workspace_id
    )

    assert result.repaired_count == 1
    assert result.purged_count == 0
    _assert_owned_and_readable(installed_blob)


def test_a_collection_that_meets_an_owner_restores_instead_of_failing(
    installed_blob: _InstalledFixture, monkeypatch
) -> None:
    """The candidate path: queued, claimed, then found owned."""

    _strand_owned_blob(installed_blob, state="gc_pending")
    now = datetime.now(UTC)
    with installed_blob.factory.begin() as session:
        session.add(
            MediaBlobGcCandidate(
                **_incarnation_key(installed_blob),
                reason="last_reference_removed",
                requested_at=now,
                eligible_at=now,
            )
        )
    service = _gc_service(installed_blob)
    # Only the candidate scan may find it here.
    monkeypatch.setattr(service, "_repair_owned_incarnations", lambda **_kw: 0)

    result = service.sweep(workspace_id=installed_blob.workspace_id)

    assert result.inspected_count == 1
    assert result.repaired_count == 1
    _assert_owned_and_readable(installed_blob)


def _register_without_the_lock(fixture: _InstalledFixture) -> None:
    """A writer that decided on stale state: owner row in, candidate out."""

    with fixture.factory.begin() as session:
        session.add(
            MediaBlobReference(
                workspace_id=fixture.workspace_id,
                owner_kind=BlobReferenceOwnerKind.LIBRARY_ITEM_VERSION.value,
                owner_id=uuid4(),
                blob_id=fixture.blob.blob_id,
                blob_epoch=fixture.blob.epoch.value,
            )
        )
        candidate = session.get(MediaBlobGcCandidate, _incarnation_key(fixture))
        if candidate is not None:
            session.delete(candidate)


def test_an_owner_appearing_after_the_trash_step_gets_its_bytes_back(
    installed_blob: _InstalledFixture, monkeypatch
) -> None:
    store = installed_blob.store
    real_trash = store.trash

    def trash_then_register(key, *, fence):
        real_trash(key, fence=fence)
        _register_without_the_lock(installed_blob)

    monkeypatch.setattr(store, "trash", trash_then_register)

    result = _gc_service(installed_blob).sweep(
        workspace_id=installed_blob.workspace_id
    )

    assert result.repaired_count == 1
    assert result.purged_count == 0
    _assert_owned_and_readable(installed_blob)


def test_an_owner_appearing_after_the_purge_marks_the_blob_missing(
    installed_blob: _InstalledFixture, monkeypatch
) -> None:
    """The bytes are gone: the state says so, and the sweep keeps going.

    Removing that owner later queues the purge that releases the quota charge.
    """

    store = installed_blob.store
    real_purge = store.purge

    def purge_then_register(key, *, fence, target):
        deletion = real_purge(key, fence=fence, target=target)
        _register_without_the_lock(installed_blob)
        return deletion

    monkeypatch.setattr(store, "purge", purge_then_register)
    result = _gc_service(installed_blob).sweep(
        workspace_id=installed_blob.workspace_id
    )
    monkeypatch.setattr(store, "purge", real_purge)

    assert result.purged_count == 0
    with installed_blob.factory.begin() as session:
        key = _incarnation_key(installed_blob)
        incarnation = session.get(BlobIncarnationRow, key)
        assert incarnation is not None and incarnation.state == "missing"
        assert session.get(MediaBlobGcCandidate, key) is None
        owner = session.scalar(select(MediaBlobReference))
        assert owner is not None
        assert SqlAlchemyBlobReferenceLedger(session).unregister(
            owner_kind=BlobReferenceOwnerKind(owner.owner_kind),
            owner_id=owner.owner_id,
            blob=installed_blob.blob,
        )

    replay = _gc_service(installed_blob).sweep(
        workspace_id=installed_blob.workspace_id
    )

    assert replay.purged_count == 1
    with installed_blob.factory() as session:
        incarnation = session.get(BlobIncarnationRow, _incarnation_key(installed_blob))
        quota = session.get(WorkspaceMediaQuotaUsage, installed_blob.workspace_id)
        assert incarnation is not None and incarnation.state == "purged"
        assert quota is not None and quota.charged_bytes == 0

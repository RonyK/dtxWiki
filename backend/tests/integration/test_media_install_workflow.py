from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from threading import Barrier
from uuid import NAMESPACE_URL, uuid4, uuid5

import pytest
from sqlalchemy import select

from backend.app.container import assemble
from backend.app.db import make_engine, make_session_factory
from backend.app.settings import Settings
from backend.media.adapters.sqlalchemy import SqlAlchemyMediaInstallJournal
from backend.media.adapters.sqlalchemy.install import (
    DEFAULT_STAGING_SESSION_MAX_BYTES,
)
from backend.media.adapters.sqlalchemy.reconcile import (
    SqlAlchemyMediaInstallReconciler,
)
from backend.media.application import InstallValidatedBlob
from backend.media.application.install_validated import _operation_fingerprint
from backend.media.domain import (
    AvailableBlobCandidate,
    BlobEpoch,
    BlobReference,
    MediaStagingBytesExceededError,
    MediaStagingLimitExceededError,
    Sha256Digest,
)
from backend.media.ports.contracts import (
    IdempotencyConflictError,
    PhysicalChargeTarget,
    PhysicalObjectKind,
    StorageKey,
)
from backend.media.ports.install import MediaInstallInProgressError
from backend.models.base import Base
from backend.models.media import (
    BlobIncarnationRow,
    MediaDeletionReceipt,
    MediaInstallRequest,
    MediaOutboxRow,
    MediaPhysicalCharge,
    MediaStagingLease,
    SecurityBlockedBlobRow,
    StoredBlobInspectionRow,
    StoredBlobRow,
    WorkspaceMediaQuotaUsage,
)
from backend.models.workspace import Workspace
from backend.scripts.seed_dev import seed_default_workspace
from backend.services.library_blob_storage import FilesystemLibraryBlobStore


def _workspace_id(session_factory):
    with session_factory() as session:
        workspace_id = session.scalar(select(Workspace.id).limit(1))
    assert workspace_id is not None
    return workspace_id


def _installer(store, journal):
    return InstallValidatedBlob(
        stager=store,
        publisher=store,
        deleter=store,
        layout=store,
        journal=journal,
    )


def _execute(
    installer,
    *,
    workspace_id,
    content=b"validated image",
    actor_id=None,
    idempotency_key="context:test-asset",
    session_key="context:test-session",
    max_bytes=1024,
    canonical_media_type="image/png",
    format_id="png",
    detector_id="test-signature",
    detector_version="1",
    registry_version="test-registry-v1",
    validation_schema_version="test-schema-v1",
    metadata=None,
    admit_new_operation=True,
):
    return installer.execute(
        workspace_id=workspace_id,
        actor_id=actor_id or uuid4(),
        session_key=session_key,
        idempotency_key=idempotency_key,
        content=content,
        max_bytes=max_bytes,
        canonical_media_type=canonical_media_type,
        format_id=format_id,
        detector_id=detector_id,
        detector_version=detector_version,
        registry_version=registry_version,
        validation_schema_version=validation_schema_version,
        metadata=metadata,
        admit_new_operation=admit_new_operation,
    )


def _media_snapshot(session_factory, workspace_id):
    with session_factory() as session:
        quota = session.get(WorkspaceMediaQuotaUsage, workspace_id)
        return {
            "leases": tuple(
                session.execute(
                    select(
                        MediaStagingLease.id,
                        MediaStagingLease.state,
                        MediaStagingLease.byte_size,
                        MediaStagingLease.sha256,
                    ).order_by(MediaStagingLease.id)
                ).all()
            ),
            "installs": tuple(
                session.execute(
                    select(
                        MediaInstallRequest.id,
                        MediaInstallRequest.state,
                    ).order_by(MediaInstallRequest.id)
                ).all()
            ),
            "charges": tuple(
                session.execute(
                    select(
                        MediaPhysicalCharge.id,
                        MediaPhysicalCharge.accounting_kind,
                        MediaPhysicalCharge.state,
                    ).order_by(MediaPhysicalCharge.id)
                ).all()
            ),
            "blobs": tuple(
                session.execute(
                    select(StoredBlobRow.id, StoredBlobRow.sha256).order_by(
                        StoredBlobRow.id
                    )
                ).all()
            ),
            "incarnations": tuple(
                session.execute(
                    select(
                        BlobIncarnationRow.blob_id,
                        BlobIncarnationRow.blob_epoch,
                        BlobIncarnationRow.state,
                    ).order_by(
                        BlobIncarnationRow.blob_id,
                        BlobIncarnationRow.blob_epoch,
                    )
                ).all()
            ),
            "inspections": tuple(
                session.scalars(
                    select(StoredBlobInspectionRow.id).order_by(
                        StoredBlobInspectionRow.id
                    )
                ).all()
            ),
            "quota": (
                quota.charged_bytes,
                quota.revision,
            )
            if quota is not None
            else None,
        }


def _stored_files(store):
    return {
        path.relative_to(store.root).as_posix()
        for path in store.root.rglob("*")
        if path.is_file()
    }


class _FailFirstCompletion:
    def __init__(self, inner):
        self.inner = inner
        self.failed = False

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def complete_install(self, pending, *, publish, staging_deletion):
        if not self.failed:
            self.failed = True
            raise RuntimeError("simulated crash after publish")
        return self.inner.complete_install(
            pending,
            publish=publish,
            staging_deletion=staging_deletion,
        )


class _StageProbe:
    def __init__(
        self,
        inner,
        *,
        crash_before_stage=False,
        crash_after_stage=False,
    ):
        self.inner = inner
        self.crash_before_stage = crash_before_stage
        self.crash_after_stage = crash_after_stage
        self.stage_calls = 0
        self.staged = None

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def stage(self, chunks, *, target, max_bytes):
        self.stage_calls += 1
        if self.crash_before_stage:
            raise RuntimeError("simulated crash before physical stage")
        self.staged = self.inner.stage(
            chunks,
            target=target,
            max_bytes=max_bytes,
        )
        if self.crash_after_stage:
            raise RuntimeError("simulated crash before staging acknowledgement")
        return self.staged


def _expire_media_install(session_factory, *, workspace_id, now):
    with session_factory.begin() as session:
        lease = session.scalar(
            select(MediaStagingLease).where(
                MediaStagingLease.workspace_id == workspace_id
            )
        )
        assert lease is not None
        lease.expires_at = now - timedelta(seconds=1)
        lease.updated_at = now - timedelta(minutes=2)
        request = session.scalar(
            select(MediaInstallRequest).where(
                MediaInstallRequest.workspace_id == workspace_id
            )
        )
        assert request is not None
        request.claim_token = None
        request.claim_expires_at = None


def test_media_install_replays_after_publish_before_database_finalize(
    session_factory,
    tmp_path,
) -> None:
    workspace_id = _workspace_id(session_factory)
    store = FilesystemLibraryBlobStore(tmp_path / "media")
    journal = SqlAlchemyMediaInstallJournal(session_factory)
    faulting = _FailFirstCompletion(journal)
    installer = _installer(store, faulting)
    actor_id = uuid4()

    with pytest.raises(RuntimeError, match="simulated crash"):
        _execute(installer, workspace_id=workspace_id, actor_id=actor_id)

    with session_factory() as session:
        request = session.scalar(select(MediaInstallRequest))
        incarnation = session.scalar(select(BlobIncarnationRow))
        staging_charge = session.scalar(
            select(MediaPhysicalCharge).where(
                MediaPhysicalCharge.accounting_kind == "staging"
            )
        )
    assert request is not None and request.state == "pending"
    assert incarnation is not None and incarnation.state == "pending_install"
    assert staging_charge is not None and staging_charge.state == "charged"
    assert store.path_for_key(incarnation.storage_key).is_file()

    available = _execute(installer, workspace_id=workspace_id, actor_id=actor_id)

    handle = store.open(StorageKey(available.storage_key))
    try:
        assert handle.read() == b"validated image"
    finally:
        handle.close()
    with session_factory() as session:
        request = session.scalar(select(MediaInstallRequest))
        incarnation = session.scalar(select(BlobIncarnationRow))
        charges = session.scalars(select(MediaPhysicalCharge)).all()
        topics = session.scalars(
            select(MediaOutboxRow.topic).order_by(MediaOutboxRow.topic)
        ).all()
        quota = session.get(WorkspaceMediaQuotaUsage, workspace_id)
    assert request is not None and request.state == "available"
    assert incarnation is not None and incarnation.state == "available"
    assert sorted((charge.accounting_kind, charge.state) for charge in charges) == [
        ("available", "charged"),
        ("staging", "released"),
    ]
    assert topics == ["BlobAvailable", "InstallRequested"]
    assert quota is not None and quota.charged_bytes == len(b"validated image")
    assert list(store.staging_root.rglob("*.stage")) == []


def test_media_install_exact_replay_uses_bound_result_after_size_policy_change(
    session_factory,
    tmp_path,
) -> None:
    workspace_id = _workspace_id(session_factory)
    actor_id = uuid4()
    store = FilesystemLibraryBlobStore(tmp_path / "media-policy-replay")
    installer = _installer(store, SqlAlchemyMediaInstallJournal(session_factory))

    original = _execute(
        installer,
        workspace_id=workspace_id,
        actor_id=actor_id,
        content=b"policy-bound-bytes",
        max_bytes=1024,
        metadata={"width": 12, "animated": False},
    )
    before = _media_snapshot(session_factory, workspace_id)
    files_before = _stored_files(store)

    replay = _execute(
        installer,
        workspace_id=workspace_id,
        actor_id=actor_id,
        content=b"policy-bound-bytes",
        max_bytes=1,
        metadata={"animated": False, "width": 12},
    )

    assert replay == original
    assert _media_snapshot(session_factory, workspace_id) == before
    assert _stored_files(store) == files_before


def test_media_install_policy_gate_rejects_only_new_operation_before_staging(
    session_factory,
    tmp_path,
) -> None:
    workspace_id = _workspace_id(session_factory)
    actor_id = uuid4()
    store = _StageProbe(FilesystemLibraryBlobStore(tmp_path / "media-policy-gate"))
    installer = _installer(store, SqlAlchemyMediaInstallJournal(session_factory))

    with pytest.raises(ValueError, match="^media_policy_changed$"):
        _execute(
            installer,
            workspace_id=workspace_id,
            actor_id=actor_id,
            content=b"policy-gated-bytes",
            admit_new_operation=False,
        )

    assert store.stage_calls == 0
    with session_factory() as session:
        assert session.scalar(select(MediaStagingLease)) is None
        assert session.scalar(select(MediaPhysicalCharge)) is None
        assert session.scalar(select(MediaInstallRequest)) is None

    original = _execute(
        installer,
        workspace_id=workspace_id,
        actor_id=actor_id,
        content=b"policy-gated-bytes",
    )
    before = _media_snapshot(session_factory, workspace_id)
    files_before = _stored_files(store)
    calls_before = store.stage_calls

    replay = _execute(
        installer,
        workspace_id=workspace_id,
        actor_id=actor_id,
        content=b"policy-gated-bytes",
        admit_new_operation=False,
    )

    assert replay == original
    assert store.stage_calls == calls_before
    assert _media_snapshot(session_factory, workspace_id) == before
    assert _stored_files(store) == files_before


def test_media_install_rejects_same_operation_with_globally_available_digest(
    session_factory,
    tmp_path,
) -> None:
    workspace_id = _workspace_id(session_factory)
    actor_id = uuid4()
    store = FilesystemLibraryBlobStore(tmp_path / "media-conflicting-digest")
    installer = _installer(store, SqlAlchemyMediaInstallJournal(session_factory))

    _execute(
        installer,
        workspace_id=workspace_id,
        actor_id=actor_id,
        content=b"operation-a",
        idempotency_key="context:operation-a",
    )
    _execute(
        installer,
        workspace_id=workspace_id,
        actor_id=actor_id,
        content=b"already-available-b",
        idempotency_key="context:operation-b",
    )
    before = _media_snapshot(session_factory, workspace_id)
    files_before = _stored_files(store)

    with pytest.raises(IdempotencyConflictError, match="media_install_operation"):
        _execute(
            installer,
            workspace_id=workspace_id,
            actor_id=actor_id,
            content=b"already-available-b",
            idempotency_key="context:operation-a",
        )

    assert _media_snapshot(session_factory, workspace_id) == before
    assert _stored_files(store) == files_before


def test_media_install_concurrent_identical_replays_converge_on_sqlite(
    tmp_path,
) -> None:
    database_url = (
        f"sqlite+pysqlite:///{(tmp_path / 'media-identical-replay.sqlite').as_posix()}"
    )
    engine = make_engine(database_url)
    Base.metadata.create_all(engine)
    session_factory = make_session_factory(engine)
    now = datetime.now(UTC)
    with session_factory.begin() as session:
        workspace = seed_default_workspace(session)
        workspace_id = workspace.id
        session.add(
            WorkspaceMediaQuotaUsage(
                workspace_id=workspace_id,
                limit_bytes=1024,
                charged_bytes=0,
                revision=0,
                updated_at=now,
            )
        )

    store = FilesystemLibraryBlobStore(tmp_path / "media-concurrent-replay")
    installer = _installer(store, SqlAlchemyMediaInstallJournal(session_factory))
    actor_id = uuid4()
    start = Barrier(2)

    def install():
        start.wait(timeout=10)
        return _execute(
            installer,
            workspace_id=workspace_id,
            actor_id=actor_id,
            content=b"concurrent-identical",
            idempotency_key="context:concurrent-identical",
            session_key="context:concurrent-session",
            metadata={"width": 12},
        )

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(install) for _ in range(2)]
            results = [future.result(timeout=20) for future in futures]

        assert results[0] == results[1]
        snapshot = _media_snapshot(session_factory, workspace_id)
        assert len(snapshot["leases"]) == 1
        assert len(snapshot["installs"]) == 1
        assert len(snapshot["blobs"]) == 1
        assert len(snapshot["incarnations"]) == 1
        assert len(snapshot["inspections"]) == 1
        assert sorted((row[1], row[2]) for row in snapshot["charges"]) == [
            ("available", "charged"),
            ("staging", "released"),
        ]
        assert snapshot["quota"] is not None
        assert snapshot["quota"][0] == len(b"concurrent-identical")
        assert list(store.staging_root.rglob("*.stage")) == []
    finally:
        engine.dispose()


def test_media_install_staging_claim_allows_takeover_after_ttl(
    session_factory,
    tmp_path,
) -> None:
    workspace_id = _workspace_id(session_factory)
    actor_id = uuid4()
    content = b"abandoned-staging-owner"
    digest = Sha256Digest(sha256(content).hexdigest())
    blob_id = uuid5(NAMESPACE_URL, f"media-blob:{workspace_id}:{digest.value}")
    blob = BlobReference(workspace_id, blob_id, BlobEpoch(1))
    store = FilesystemLibraryBlobStore(tmp_path / "media-staging-takeover")
    destination = store.blob_key(blob, digest, publish_generation=1)
    candidate = AvailableBlobCandidate(
        workspace_id=workspace_id,
        sha256=digest,
        byte_size=len(content),
        storage_key=destination.value,
        canonical_media_type="image/png",
        format_id="png",
        detector_id="test-signature",
        detector_version="1",
        registry_version="test-registry-v1",
        validation_schema_version="test-schema-v1",
        metadata={},
    )
    idempotency_key = "context:abandoned-owner"
    request_id = uuid5(
        NAMESPACE_URL,
        f"media-install:{workspace_id}:{idempotency_key}",
    )
    fingerprint = _operation_fingerprint(
        candidate,
        actor_id=actor_id,
        session_key="context:abandoned-session",
        idempotency_key=idempotency_key,
        request_id=request_id,
    )
    target = PhysicalChargeTarget(
        workspace_id=workspace_id,
        kind=PhysicalObjectKind.STAGING_LEASE,
        object_id=uuid5(
            NAMESPACE_URL,
            f"media-stage:{request_id}:{fingerprint}",
        ),
        generation=1,
    )
    staging_key = store.staging_key(target)
    charge_id = uuid5(
        NAMESPACE_URL,
        f"media-stage-charge:{workspace_id}:{target.object_id}:1",
    )
    journal = SqlAlchemyMediaInstallJournal(session_factory)
    started_at = datetime(2025, 1, 1, tzinfo=UTC)

    def prepare(now):
        return journal.prepare_operation(
            candidate=candidate,
            target=target,
            actor_id=actor_id,
            session_key="context:abandoned-session",
            idempotency_key=idempotency_key,
            request_id=request_id,
            storage_key=staging_key,
            charge_id=charge_id,
            proposed_blob_id=blob_id,
            proposed_blob_epoch=1,
            publish_generation=1,
            admission_max_bytes=1024,
            now=now,
            expires_at=now + timedelta(hours=1),
        )

    first = prepare(started_at)
    assert first.owns_staging is True
    assert prepare(started_at + timedelta(seconds=29)).in_progress is True
    takeover = prepare(started_at + timedelta(seconds=31))
    assert takeover.owns_staging is True
    assert first.claim_fence is not None
    assert takeover.claim_fence is not None
    assert takeover.claim_fence != first.claim_fence

    staged = store.stage((content,), target=target, max_bytes=len(content))
    with pytest.raises(MediaInstallInProgressError):
        journal.record_staged(
            staged,
            charge_id=charge_id,
            operation_claim=first.claim_fence,
        )
    journal.record_staged(
        staged,
        charge_id=charge_id,
        operation_claim=takeover.claim_fence,
    )


def test_media_install_quota_rejection_happens_before_staging(
    session_factory,
    tmp_path,
) -> None:
    workspace_id = _workspace_id(session_factory)
    now = datetime.now(UTC)
    with session_factory.begin() as session:
        session.add(
            WorkspaceMediaQuotaUsage(
                workspace_id=workspace_id,
                limit_bytes=1,
                charged_bytes=0,
                revision=0,
                updated_at=now,
            )
        )
    store = _StageProbe(FilesystemLibraryBlobStore(tmp_path / "media-quota"))
    installer = _installer(store, SqlAlchemyMediaInstallJournal(session_factory))

    with pytest.raises(ValueError, match="media_quota_exceeded"):
        _execute(installer, workspace_id=workspace_id, content=b"too large")

    with session_factory() as session:
        lease = session.scalar(select(MediaStagingLease))
        charge = session.scalar(select(MediaPhysicalCharge))
        install = session.scalar(select(MediaInstallRequest))
        quota = session.get(WorkspaceMediaQuotaUsage, workspace_id)
    assert store.stage_calls == 0
    assert lease is None
    assert charge is None
    assert install is None
    assert quota is not None and quota.charged_bytes == 0
    assert list(store.staging_root.rglob("*.stage")) == []


def test_media_install_crash_after_stage_keeps_durable_admission_for_reconcile(
    session_factory,
    tmp_path,
) -> None:
    workspace_id = _workspace_id(session_factory)
    content = b"crash-after-physical-stage"
    raw_store = FilesystemLibraryBlobStore(tmp_path / "media-stage-crash")
    store = _StageProbe(raw_store, crash_after_stage=True)
    journal = SqlAlchemyMediaInstallJournal(session_factory)
    installer = _installer(store, journal)
    actor_id = uuid4()

    with pytest.raises(
        RuntimeError,
        match="simulated crash before staging acknowledgement",
    ):
        _execute(
            installer,
            workspace_id=workspace_id,
            actor_id=actor_id,
            content=content,
            idempotency_key="context:crash-after-stage",
        )

    assert store.stage_calls == 1
    assert store.staged is not None
    with session_factory() as session:
        lease = session.scalar(select(MediaStagingLease))
        charge = session.scalar(select(MediaPhysicalCharge))
        request = session.scalar(select(MediaInstallRequest))
        incarnation = session.scalar(select(BlobIncarnationRow))
        outbox = session.scalar(select(MediaOutboxRow))
        quota = session.get(WorkspaceMediaQuotaUsage, workspace_id)
    assert lease is not None and lease.state == "reserved"
    assert lease.byte_size == len(content)
    assert lease.sha256 == sha256(content).hexdigest()
    assert charge is not None and charge.state == "charged"
    assert charge.byte_size == len(content)
    assert request is not None and request.state == "pending"
    assert request.physical_charge_id == charge.id
    assert incarnation is not None and incarnation.state == "pending_install"
    assert outbox is not None and outbox.topic == "InstallRequested"
    assert quota is not None and quota.charged_bytes == len(content)
    assert len(list(raw_store.staging_root.rglob("*.stage"))) == 1

    reconcile_at = datetime.now(UTC)
    _expire_media_install(
        session_factory,
        workspace_id=workspace_id,
        now=reconcile_at,
    )
    reconciler = SqlAlchemyMediaInstallReconciler(
        store_for=lambda _workspace_id: raw_store,
    )
    with session_factory.begin() as session:
        result = reconciler.sweep_expired(
            session,
            workspace_id=workspace_id,
            now=reconcile_at,
        )

    with session_factory() as session:
        lease = session.scalar(select(MediaStagingLease))
        charges = session.scalars(select(MediaPhysicalCharge)).all()
        request = session.scalar(select(MediaInstallRequest))
        incarnation = session.scalar(select(BlobIncarnationRow))
        receipt = session.scalar(select(MediaDeletionReceipt))
        quota = session.get(WorkspaceMediaQuotaUsage, workspace_id)
    assert result.inspected_count == 1
    assert result.resumed_count == 1
    assert result.cleaned_count == 0
    assert lease is not None and lease.state == "deleted"
    assert sorted((row.accounting_kind, row.state) for row in charges) == [
        ("available", "charged"),
        ("staging", "released"),
    ]
    assert request is not None and request.state == "available"
    assert incarnation is not None and incarnation.state == "available"
    assert receipt is not None
    assert receipt.physical_charge_id == charge.id
    assert receipt.target_generation == store.staged.target.generation
    assert quota is not None and quota.charged_bytes == len(content)
    assert list(raw_store.staging_root.rglob("*.stage")) == []

    replay = _execute(
        _installer(raw_store, journal),
        workspace_id=workspace_id,
        actor_id=actor_id,
        content=content,
        idempotency_key="context:crash-after-stage",
        admit_new_operation=False,
    )
    assert replay.reference.blob_id == request.blob_id
    with session_factory.begin() as session:
        second = reconciler.sweep_expired(
            session,
            workspace_id=workspace_id,
            now=reconcile_at + timedelta(minutes=1),
        )
    assert second.inspected_count == 0


def test_media_install_reconciler_cleans_missing_stage_with_stable_replay(
    session_factory,
    tmp_path,
) -> None:
    workspace_id = _workspace_id(session_factory)
    actor_id = uuid4()
    content = b"missing-before-physical-stage"
    raw_store = FilesystemLibraryBlobStore(tmp_path / "media-stage-missing")
    store = _StageProbe(raw_store, crash_before_stage=True)
    journal = SqlAlchemyMediaInstallJournal(session_factory)

    with pytest.raises(RuntimeError, match="simulated crash before physical stage"):
        _execute(
            _installer(store, journal),
            workspace_id=workspace_id,
            actor_id=actor_id,
            content=content,
            idempotency_key="context:missing-stage",
        )

    reconcile_at = datetime.now(UTC)
    _expire_media_install(
        session_factory,
        workspace_id=workspace_id,
        now=reconcile_at,
    )
    reconciler = SqlAlchemyMediaInstallReconciler(
        store_for=lambda _workspace_id: raw_store,
    )
    with session_factory.begin() as session:
        result = reconciler.sweep_expired(
            session,
            workspace_id=workspace_id,
            now=reconcile_at,
        )

    with session_factory() as session:
        lease = session.scalar(select(MediaStagingLease))
        charge = session.scalar(select(MediaPhysicalCharge))
        request = session.scalar(select(MediaInstallRequest))
        incarnation = session.scalar(select(BlobIncarnationRow))
        receipt = session.scalar(select(MediaDeletionReceipt))
        quota = session.get(WorkspaceMediaQuotaUsage, workspace_id)
    assert result.cleaned_count == 1
    assert lease is not None and lease.state == "deleted"
    assert charge is not None and charge.state == "released"
    assert request is not None and request.state == "failed"
    assert request.last_failure_code == "media_staging_missing"
    assert incarnation is not None and incarnation.state == "missing"
    assert receipt is not None and receipt.physical_charge_id == charge.id
    assert quota is not None and quota.charged_bytes == 0
    assert [path for path in raw_store.root.rglob("*") if path.is_file()] == []

    with session_factory.begin() as session:
        second = reconciler.sweep_expired(
            session,
            workspace_id=workspace_id,
            now=reconcile_at + timedelta(minutes=1),
        )
    assert second.inspected_count == 0

    with pytest.raises(ValueError, match="^media_staging_missing$"):
        _execute(
            _installer(raw_store, journal),
            workspace_id=workspace_id,
            actor_id=actor_id,
            content=content,
            idempotency_key="context:missing-stage",
            admit_new_operation=False,
        )

    with session_factory() as session:
        charges = session.scalars(select(MediaPhysicalCharge)).all()
        receipts = session.scalars(select(MediaDeletionReceipt)).all()
        quota = session.get(WorkspaceMediaQuotaUsage, workspace_id)
    assert [(row.id, row.state) for row in charges] == [(charge.id, "released")]
    assert [row.id for row in receipts] == [receipt.id]
    assert quota is not None and quota.charged_bytes == 0
    assert [path for path in raw_store.root.rglob("*") if path.is_file()] == []


def test_media_install_concurrent_reconcilers_resume_expired_stage_once(
    tmp_path,
) -> None:
    database_url = (
        f"sqlite+pysqlite:///{(tmp_path / 'media-reconcile-race.sqlite').as_posix()}"
    )
    engine = make_engine(database_url)
    Base.metadata.create_all(engine)
    session_factory = make_session_factory(engine)
    now = datetime.now(UTC)
    with session_factory.begin() as session:
        workspace = seed_default_workspace(session)
        workspace_id = workspace.id
        session.add(
            WorkspaceMediaQuotaUsage(
                workspace_id=workspace_id,
                limit_bytes=1024,
                charged_bytes=0,
                revision=0,
                updated_at=now,
            )
        )

    raw_store = FilesystemLibraryBlobStore(tmp_path / "media-reconcile-race")
    probe = _StageProbe(raw_store, crash_after_stage=True)
    journal = SqlAlchemyMediaInstallJournal(session_factory)
    actor_id = uuid4()
    content = b"concurrent-reconcile"
    with pytest.raises(RuntimeError, match="simulated crash"):
        _execute(
            _installer(probe, journal),
            workspace_id=workspace_id,
            actor_id=actor_id,
            content=content,
            idempotency_key="context:concurrent-reconcile",
        )
    reconcile_at = datetime.now(UTC)
    _expire_media_install(
        session_factory,
        workspace_id=workspace_id,
        now=reconcile_at,
    )
    reconciler = SqlAlchemyMediaInstallReconciler(
        store_for=lambda _workspace_id: raw_store,
    )
    start = Barrier(2)

    def reconcile():
        start.wait(timeout=10)
        with session_factory.begin() as session:
            return reconciler.sweep_expired(
                session,
                workspace_id=workspace_id,
                now=reconcile_at,
            )

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(reconcile) for _ in range(2)]
            results = [future.result(timeout=20) for future in futures]

        assert sum(result.resumed_count for result in results) == 1
        assert sum(result.cleaned_count for result in results) == 0
        with session_factory() as session:
            request = session.scalar(select(MediaInstallRequest))
            charges = session.scalars(select(MediaPhysicalCharge)).all()
            receipts = session.scalars(select(MediaDeletionReceipt)).all()
            quota = session.get(WorkspaceMediaQuotaUsage, workspace_id)
        assert request is not None and request.state == "available"
        assert sorted((row.accounting_kind, row.state) for row in charges) == [
            ("available", "charged"),
            ("staging", "released"),
        ]
        assert len(receipts) == 1
        assert quota is not None and quota.charged_bytes == len(content)
        replay = _execute(
            _installer(raw_store, journal),
            workspace_id=workspace_id,
            actor_id=actor_id,
            content=content,
            idempotency_key="context:concurrent-reconcile",
            admit_new_operation=False,
        )
        assert replay.reference.blob_id == request.blob_id
    finally:
        engine.dispose()


def _reserve_lease(
    journal: SqlAlchemyMediaInstallJournal,
    *,
    workspace_id,
    actor_id,
    session_key: str,
    byte_size: int,
    now: datetime,
) -> None:
    lease_id = uuid4()
    journal.reserve_staging(
        target=PhysicalChargeTarget(
            workspace_id=workspace_id,
            kind=PhysicalObjectKind.STAGING_LEASE,
            object_id=lease_id,
            generation=1,
        ),
        actor_id=actor_id,
        session_key=session_key,
        idempotency_key=f"test:{lease_id}",
        storage_key=StorageKey(f"stage/{workspace_id.hex}/{lease_id.hex}/1"),
        max_bytes=byte_size,
        now=now,
        expires_at=now + timedelta(hours=1),
    )


def test_media_staging_enforces_actor_count_and_byte_caps(session_factory) -> None:
    workspace_id = _workspace_id(session_factory)
    actor_id = uuid4()
    now = datetime.now(UTC)
    count_limited = SqlAlchemyMediaInstallJournal(
        session_factory,
        actor_max_count=1,
        session_max_count=10,
        actor_max_bytes=100,
        session_max_bytes=100,
        actor_rate_per_minute=10,
        session_rate_per_minute=10,
    )
    _reserve_lease(
        count_limited,
        workspace_id=workspace_id,
        actor_id=actor_id,
        session_key="count:first",
        byte_size=4,
        now=now,
    )
    with pytest.raises(MediaStagingLimitExceededError):
        _reserve_lease(
            count_limited,
            workspace_id=workspace_id,
            actor_id=actor_id,
            session_key="count:second",
            byte_size=4,
            now=now,
        )

    other_actor = uuid4()
    byte_limited = SqlAlchemyMediaInstallJournal(
        session_factory,
        actor_max_count=10,
        session_max_count=10,
        actor_max_bytes=10,
        session_max_bytes=100,
        actor_rate_per_minute=10,
        session_rate_per_minute=10,
    )
    _reserve_lease(
        byte_limited,
        workspace_id=workspace_id,
        actor_id=other_actor,
        session_key="bytes:first",
        byte_size=6,
        now=now,
    )
    with pytest.raises(MediaStagingBytesExceededError):
        _reserve_lease(
            byte_limited,
            workspace_id=workspace_id,
            actor_id=other_actor,
            session_key="bytes:second",
            byte_size=5,
            now=now,
        )


def test_media_staging_enforces_session_count_and_rate_caps(session_factory) -> None:
    workspace_id = _workspace_id(session_factory)
    now = datetime.now(UTC)
    session_limited = SqlAlchemyMediaInstallJournal(
        session_factory,
        actor_max_count=10,
        session_max_count=1,
        actor_max_bytes=100,
        session_max_bytes=100,
        actor_rate_per_minute=10,
        session_rate_per_minute=10,
    )
    _reserve_lease(
        session_limited,
        workspace_id=workspace_id,
        actor_id=uuid4(),
        session_key="shared-session",
        byte_size=4,
        now=now,
    )
    with pytest.raises(ValueError, match="media_staging_limit_exceeded"):
        _reserve_lease(
            session_limited,
            workspace_id=workspace_id,
            actor_id=uuid4(),
            session_key="shared-session",
            byte_size=4,
            now=now,
        )

    rate_limited = SqlAlchemyMediaInstallJournal(
        session_factory,
        actor_max_count=10,
        session_max_count=10,
        actor_max_bytes=100,
        session_max_bytes=100,
        actor_rate_per_minute=1,
        session_rate_per_minute=10,
    )
    rate_actor = uuid4()
    _reserve_lease(
        rate_limited,
        workspace_id=workspace_id,
        actor_id=rate_actor,
        session_key="rate:first",
        byte_size=4,
        now=now,
    )
    with pytest.raises(ValueError, match="media_staging_limit_exceeded"):
        _reserve_lease(
            rate_limited,
            workspace_id=workspace_id,
            actor_id=rate_actor,
            session_key="rate:second",
            byte_size=4,
            now=now,
        )


def test_media_install_does_not_reuse_security_blocked_incarnation(
    session_factory,
    tmp_path,
) -> None:
    workspace_id = _workspace_id(session_factory)
    store = FilesystemLibraryBlobStore(tmp_path / "media-blocked")
    installer = _installer(store, SqlAlchemyMediaInstallJournal(session_factory))
    available = _execute(installer, workspace_id=workspace_id)

    with session_factory.begin() as session:
        session.add(
            SecurityBlockedBlobRow(
                workspace_id=available.reference.workspace_id,
                blob_id=available.reference.blob_id,
                blob_epoch=available.reference.epoch.value,
                generation=1,
                state="quarantined",
                reason_code="integration_test_block",
            )
        )
    with session_factory() as session:
        before = {
            "leases": tuple(session.scalars(select(MediaStagingLease.id)).all()),
            "installs": tuple(session.scalars(select(MediaInstallRequest.id)).all()),
            "charges": tuple(session.scalars(select(MediaPhysicalCharge.id)).all()),
        }
    files_before = {
        path.relative_to(store.root).as_posix()
        for path in store.root.rglob("*")
        if path.is_file()
    }

    with pytest.raises(ValueError, match="security blocked"):
        _execute(
            installer,
            workspace_id=workspace_id,
            idempotency_key="context:blocked-reuse",
        )

    with session_factory() as session:
        after = {
            "leases": tuple(session.scalars(select(MediaStagingLease.id)).all()),
            "installs": tuple(session.scalars(select(MediaInstallRequest.id)).all()),
            "charges": tuple(session.scalars(select(MediaPhysicalCharge.id)).all()),
        }
    files_after = {
        path.relative_to(store.root).as_posix()
        for path in store.root.rglob("*")
        if path.is_file()
    }
    assert after == before
    assert files_after == files_before


def test_container_conversion_installer_admits_65_without_weakening_context_rate(
    session_factory,
    tmp_path,
) -> None:
    workspace_id = _workspace_id(session_factory)
    registry = assemble(
        Settings(
            database_url="sqlite+pysqlite:///:memory:",
            library_blob_root=str(tmp_path / "profile-media"),
        ),
        session_factory=session_factory,
    ).get_services()
    conversion_factory = registry.library_conversion.durable_media_installer_factory
    assert conversion_factory is not None
    conversion_installer = conversion_factory(session_factory, workspace_id)
    conversion_actor = uuid4()

    for index in range(65):
        _execute(
            conversion_installer,
            workspace_id=workspace_id,
            content=f"conversion-output-{index}".encode(),
            actor_id=conversion_actor,
            idempotency_key=f"library-conversion:profile:{index}",
            session_key="library-conversion:profile",
        )

    ordinary_installer = registry.durable_media_installer_factory(
        session_factory, workspace_id
    )
    ordinary_actor = uuid4()
    for index in range(30):
        _execute(
            ordinary_installer,
            workspace_id=workspace_id,
            content=f"ordinary-context-output-{index}".encode(),
            actor_id=ordinary_actor,
            idempotency_key=f"context:profile:{index}",
            session_key="context:profile",
        )
    with pytest.raises(ValueError, match="media_staging_limit_exceeded"):
        _execute(
            ordinary_installer,
            workspace_id=workspace_id,
            content=b"ordinary-context-output-30",
            actor_id=ordinary_actor,
            idempotency_key="context:profile:30",
            session_key="context:profile",
        )


def test_conversion_profile_covers_configured_asset_above_ordinary_session_cap(
    session_factory,
    tmp_path,
) -> None:
    workspace_id = _workspace_id(session_factory)
    configured_max_bytes = DEFAULT_STAGING_SESSION_MAX_BYTES + 1
    registry = assemble(
        Settings(
            database_url="sqlite+pysqlite:///:memory:",
            library_blob_root=str(tmp_path / "large-profile-media"),
            library_conversion_max_asset_bytes=configured_max_bytes,
        ),
        session_factory=session_factory,
    ).get_services()
    now = datetime.now(UTC)

    conversion = registry.library_conversion.durable_media_installer_factory
    assert conversion is not None
    _reserve_lease(
        conversion(session_factory, workspace_id)._journal,
        workspace_id=workspace_id,
        actor_id=uuid4(),
        session_key="library-conversion:large-output",
        byte_size=configured_max_bytes,
        now=now,
    )

    ordinary = registry.durable_media_installer_factory(session_factory, workspace_id)
    with pytest.raises(ValueError, match="media_staging_bytes_exceeded"):
        _reserve_lease(
            ordinary._journal,
            workspace_id=workspace_id,
            actor_id=uuid4(),
            session_key="context:large-output",
            byte_size=configured_max_bytes,
            now=now,
        )

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from threading import Barrier, Event, Thread
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker

import backend.worker.runtime as worker_runtime
from backend.library_media.adapters.sqlalchemy import (
    SqlAlchemyLibraryBlobRepository,
    SqlAlchemyLibraryLogicalQuota,
)
from backend.media.adapters.sqlalchemy import SqlAlchemyMediaInstallJournal
from backend.media.adapters.sqlalchemy.quota import SqlAlchemyPhysicalQuota
from backend.media.adapters.sqlalchemy.repository import SqlAlchemyMediaRepository
from backend.media.application import InstallValidatedBlob
from backend.models.audit import AuditEvent
from backend.models.base import Base, utc_now
from backend.models.chunk import ContentChunk
from backend.models.job import Job
from backend.models.library import (
    LibraryArtifact,
    LibraryAttempt,
    LibraryAttemptConversionDetail,
    LibraryItem,
    LibraryItemVersion,
    LibraryLogicalCharge,
    LibraryLogicalQuotaUsage,
    LibraryProjectionOutbox,
    LibraryRun,
    LibraryRunConversionDetail,
)
from backend.models.media import (
    BlobIncarnationRow,
    MediaInstallRequest,
    MediaOutboxRow,
    MediaPhysicalCharge,
    StoredBlobInspectionRow,
    StoredBlobRow,
)
from backend.models.workspace import Workspace
from backend.scripts.seed_dev import seed_default_workspace
from backend.services.conversion_orchestrator import (
    LIBRARY_CONVERSION_JOB_TYPE,
    MAX_DURABLE_CONVERSION_OUTPUTS,
    LibraryConversionOrchestrator,
    LibraryConversionRetryNotAvailableError,
    get_or_create_library_blob,
)
from backend.services.converter_registry import ConverterRegistry
from backend.services.library_blob_storage import FilesystemLibraryBlobStore
from backend.spi.conversion import (
    CONVERTER_SPI_VERSION,
    ConversionError,
    ConversionLimits,
    ConversionRequest,
    ConversionResult,
    ConverterDescriptor,
    ProbeRequest,
    ProbeResult,
    ProducedAsset,
)
from backend.worker.runtime import OneShotWorker, _ClaimedJob

_FORMAT_ID = "testfmt"
_MEDIA_TYPE = "application/x-testfmt"
_SOURCE = b"source payload"


class _ScriptedConverter:
    def __init__(
        self,
        converter_id: str,
        *,
        priority: int,
        outcomes: tuple[ConversionResult | ConversionError, ...],
        probe_supported: bool = True,
        max_input_bytes: int | None = None,
    ) -> None:
        self._descriptor = ConverterDescriptor(
            converter_id=converter_id,
            converter_version="1.0.0",
            spi_version=CONVERTER_SPI_VERSION,
            formats=frozenset({_FORMAT_ID}),
            media_types=frozenset({_MEDIA_TYPE}),
            priority=priority,
            execution_mode="builtin",
            network_required=False,
            deterministic=True,
            license_expression="Apache-2.0",
            source_url="https://example.test/converter",
            capabilities=frozenset({"markdown"}),
            max_input_bytes=max_input_bytes,
        )
        self._outcomes = outcomes
        self._probe_supported = probe_supported
        self.probe_calls = 0
        self.convert_calls = 0

    def descriptor(self) -> ConverterDescriptor:
        return self._descriptor

    def probe(self, request: ProbeRequest) -> ProbeResult:
        assert request.format_id == _FORMAT_ID
        self.probe_calls += 1
        return ProbeResult(
            supported=self._probe_supported,
            confidence=Decimal("1.000"),
        )

    def convert(self, request: ConversionRequest) -> ConversionResult:
        assert request.source_sha256
        self.convert_calls += 1
        outcome = self._outcomes[min(self.convert_calls - 1, len(self._outcomes) - 1)]
        if isinstance(outcome, ConversionError):
            raise outcome
        return outcome


class _HealthScriptedConverter(_ScriptedConverter):
    def __init__(
        self,
        converter_id: str,
        *,
        priority: int,
        outcomes: tuple[ConversionResult | ConversionError, ...],
        ready: bool,
    ) -> None:
        super().__init__(
            converter_id,
            priority=priority,
            outcomes=outcomes,
        )
        self._descriptor = replace(
            self._descriptor,
            execution_mode="subprocess",
            distribution_digest=hashlib.sha256(converter_id.encode()).hexdigest(),
        )
        self._ready = ready
        self.healthcheck_calls = 0

    def healthcheck(self) -> tuple[bool, str | None]:
        self.healthcheck_calls += 1
        return (True, None) if self._ready else (False, "converter_health_unavailable")


@pytest.fixture
def limits() -> ConversionLimits:
    return ConversionLimits(
        max_input_bytes=1024,
        max_output_bytes=4096,
        max_asset_bytes=4096,
        timeout_seconds=5,
    )


def _orchestrator(
    tmp_path: Path,
    *plugins: _ScriptedConverter,
) -> tuple[LibraryConversionOrchestrator, FilesystemLibraryBlobStore]:
    # Keep the opaque b/... incarnation key under Windows MAX_PATH during the
    # integration suite; production storage roots are configured separately.
    store = FilesystemLibraryBlobStore(tmp_path.parent / f"lib-{uuid4().hex[:8]}")
    return (
        LibraryConversionOrchestrator(
            converter_registry=ConverterRegistry(tuple(plugins)),
            store_for=lambda _workspace_id: store,
            blob_repository_factory=_blob_repository,
            media_installer_factory=lambda session, _workspace_id: _media_installer(
                store, session
            ),
            logical_quota_factory=lambda session: SqlAlchemyLibraryLogicalQuota(
                session
            ),
        ),
        store,
    )


def _media_installer(
    store: FilesystemLibraryBlobStore,
    session: Session,
) -> InstallValidatedBlob:
    return InstallValidatedBlob(
        stager=store,
        publisher=store,
        deleter=store,
        layout=store,
        journal=SqlAlchemyMediaInstallJournal.for_session(session),
    )


def _durable_media_installer(
    store: FilesystemLibraryBlobStore,
    session_factory,
) -> InstallValidatedBlob:
    return InstallValidatedBlob(
        stager=store,
        publisher=store,
        deleter=store,
        layout=store,
        journal=SqlAlchemyMediaInstallJournal(session_factory),
    )


def _blob_repository(session: Session) -> SqlAlchemyLibraryBlobRepository:
    return SqlAlchemyLibraryBlobRepository(
        session,
        media_repository=SqlAlchemyMediaRepository(session),
        physical_quota=SqlAlchemyPhysicalQuota(session),
    )


def _persist_source(
    session: Session,
    store: FilesystemLibraryBlobStore,
) -> tuple[Workspace, LibraryItem, LibraryItemVersion]:
    workspace = session.scalar(select(Workspace).where(Workspace.slug == "default"))
    assert workspace is not None
    stored = store.put_bytes(
        _SOURCE,
        workspace_id=workspace.id,
        max_bytes=1024,
    )
    blob = get_or_create_library_blob(
        session,
        repository_factory=_blob_repository,
        workspace_id=workspace.id,
        stored=stored,
        media_type=_MEDIA_TYPE,
        format_id=_FORMAT_ID,
        detector_id="test",
    )
    item = LibraryItem(
        workspace_id=workspace.id,
        connector_type="upload",
        format_hint=_FORMAT_ID,
        uri=f"upload://{workspace.id}/{uuid4()}",
        title="Conversion fixture",
        trust_level="trusted",
        lifecycle_status="registered",
        metadata_json={},
    )
    session.add(item)
    session.flush()
    version = LibraryItemVersion(
        workspace_id=item.workspace_id,
        library_item_id=item.id,
        version_no=1,
        content_hash=stored.sha256,
        raw_storage_ref=None,
        source_blob_workspace_id=blob.reference.workspace_id,
        source_blob_id=blob.reference.blob_id,
        source_blob_epoch=blob.reference.epoch.value,
        original_filename="fixture.testfmt",
        declared_media_type=_MEDIA_TYPE,
        detected_media_type=_MEDIA_TYPE,
        format_id=_FORMAT_ID,
        byte_size=stored.byte_size,
        detection_json={"detector_id": "test", "confidence": 1.0},
        processing_status="pending",
    )
    session.add(version)
    session.flush()
    item.current_version_id = version.id
    session.flush()
    return workspace, item, version


def _schedule(
    orchestrator: LibraryConversionOrchestrator,
    session: Session,
    workspace: Workspace,
    version: LibraryItemVersion,
    limits: ConversionLimits,
):
    return orchestrator.schedule_initial(
        session=session,
        workspace_id=workspace.id,
        version=version,
        workspace_policy={"library": {"enabled_formats": [_FORMAT_ID]}},
        limits=limits,
        actor_kind="user",
        actor_id=str(uuid4()),
    )


def _add_manual_attempt(
    session: Session,
    *,
    version: LibraryItemVersion,
    template_run: LibraryRun,
    run_seq: int,
) -> tuple[LibraryRun, LibraryAttempt]:
    run = LibraryRun(
        workspace_id=version.workspace_id,
        library_item_version_id=version.id,
        stage="conversion",
        run_seq=run_seq,
        reason="manual_reconvert",
        status="queued",
        requested_by_actor_type="user",
        requested_by_actor_id=uuid4(),
        provider_chain_json=list(template_run.provider_chain_json),
        policy_revision=template_run.policy_revision,
        output_schema_version=template_run.output_schema_version,
        idempotency_key=f"manual:{uuid4()}",
    )
    run.conversion_detail = LibraryRunConversionDetail(
        error_fallback_matrix_json=dict(
            template_run.conversion_detail.error_fallback_matrix_json
        ),
        per_provider_budget_json=dict(
            template_run.conversion_detail.per_provider_budget_json
        ),
        total_budget_json=dict(template_run.conversion_detail.total_budget_json),
    )
    session.add(run)
    session.flush()
    entry = run.provider_chain_json[0]
    config_hash = hashlib.sha256(
        json.dumps(
            {
                "provider": entry,
                "budget": run.conversion_detail.per_provider_budget_json,
                "policy_revision": run.policy_revision,
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    ).hexdigest()
    attempt = LibraryAttempt(
        workspace_id=version.workspace_id,
        run_id=run.id,
        library_item_version_id=version.id,
        stage="conversion",
        provider_id=str(entry["converter_id"]),
        provider_version=str(entry["converter_version"]),
        provider_spi_version=str(entry["spi_version"]),
        config_hash=config_hash,
        input_sha256=version.content_hash,
        output_schema_version=run.output_schema_version,
        status="queued",
        chain_index=0,
        transport_retry_count=0,
        execution_generation=1,
        provenance_json={},
        metrics_json={},
        warnings_json=[],
    )
    attempt.conversion_detail = LibraryAttemptConversionDetail(
        manual_retry_disposition=None,
        disposition_reason=None,
    )
    session.add(attempt)
    session.flush()
    return run, attempt


def test_manual_conversion_retry_serializes_concurrent_requests(
    tmp_path: Path,
    limits: ConversionLimits,
) -> None:
    database_path = tmp_path / "conversion-retry-race.db"
    engine = create_engine(
        f"sqlite+pysqlite:///{database_path.as_posix()}",
        connect_args={"check_same_thread": False, "timeout": 5},
    )
    Base.metadata.create_all(engine)
    concurrent_factory = sessionmaker(bind=engine, expire_on_commit=False)
    with concurrent_factory.begin() as session:
        seed_default_workspace(session)

    plugin = _ScriptedConverter(
        "test.concurrent-manual-retry",
        priority=100,
        outcomes=(ConversionResult(markdown="# retry"),),
    )
    orchestrator, store = _orchestrator(tmp_path, plugin)
    with concurrent_factory.begin() as session:
        workspace, item, version = _persist_source(session, store)
        scheduled = _schedule(orchestrator, session, workspace, version, limits)
        assert scheduled.attempt is not None
        assert scheduled.job is not None
        scheduled.job.status = "running"
        scheduled.job.stage = "conversion"
        scheduled.job.started_at = utc_now()
        orchestrator.mark_terminal_failure(
            session=session,
            workspace_id=workspace.id,
            attempt_id=scheduled.attempt.id,
            execution_generation=scheduled.attempt.execution_generation,
            manual_retryable=True,
            safe_error_code="converter_process_failed",
        )
        workspace_id = workspace.id
        item_id = item.id
        version_id = version.id
        attempt_id = scheduled.attempt.id
        job_id = scheduled.job.id

    start = Barrier(2, timeout=30)
    results: list[str] = []

    def retry() -> None:
        start.wait(timeout=5)
        try:
            with concurrent_factory.begin() as session:
                orchestrator.retry_failed_conversion(
                    session=session,
                    workspace_id=workspace_id,
                    library_item_id=item_id,
                    version_id=version_id,
                    actor_kind="human",
                    actor_id=str(uuid4()),
                )
        except LibraryConversionRetryNotAvailableError:
            results.append("conflict")
        else:
            results.append("retried")

    threads = [Thread(target=retry), Thread(target=retry)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
        assert not thread.is_alive()

    assert sorted(results) == ["conflict", "retried"]
    with concurrent_factory() as session:
        attempt = session.get(LibraryAttempt, attempt_id)
        job = session.get(Job, job_id)
        assert attempt is not None and attempt.execution_generation == 2
        assert attempt.transport_retry_count == 0
        assert job is not None and job.target_ref["execution_generation"] == 2


def test_manual_conversion_retry_renews_transport_budget_for_transient_failure(
    session_factory,
    tmp_path: Path,
    limits: ConversionLimits,
) -> None:
    plugin = _ScriptedConverter(
        "test.manual-retry-after-transport-budget",
        priority=100,
        outcomes=(
            ConversionError(
                "converter_process_failed",
                "Converter process failed.",
                retryable=True,
                fallback_allowed=False,
            ),
            ConversionResult(markdown="# Manual retry recovered"),
        ),
    )
    orchestrator, store = _orchestrator(tmp_path, plugin)

    with session_factory.begin() as session:
        workspace, item, version = _persist_source(session, store)
        scheduled = _schedule(orchestrator, session, workspace, version, limits)
        assert scheduled.attempt is not None
        assert scheduled.job is not None
        budget_json = scheduled.run.conversion_detail.per_provider_budget_json
        max_transport_retries = budget_json["max_transport_retries"]
        assert isinstance(max_transport_retries, int)
        scheduled.attempt.transport_retry_count = max_transport_retries
        session.flush()
        orchestrator.mark_terminal_failure(
            session=session,
            workspace_id=workspace.id,
            attempt_id=scheduled.attempt.id,
            execution_generation=scheduled.attempt.execution_generation,
            manual_retryable=True,
            safe_error_code="converter_process_failed",
        )
        orchestrator.job_queue.complete(session=session, job=scheduled.job)

        retried = orchestrator.retry_failed_conversion(
            session=session,
            workspace_id=workspace.id,
            library_item_id=item.id,
            version_id=version.id,
            actor_kind="human",
            actor_id=str(uuid4()),
        )

        assert retried.attempt.execution_generation == 2
        assert retried.attempt.transport_retry_count == 0
        assert plugin.convert_calls == 0
        first_execution = orchestrator.execute_attempt(
            session=session,
            workspace_id=workspace.id,
            attempt_id=retried.attempt.id,
            execution_generation=retried.attempt.execution_generation,
        )

        assert first_execution.status == "requeued"
        assert plugin.convert_calls == 1
        assert retried.attempt.status == "queued"
        assert retried.attempt.execution_generation == 3
        assert retried.attempt.transport_retry_count == 1

        second_execution = orchestrator.execute_attempt(
            session=session,
            workspace_id=workspace.id,
            attempt_id=retried.attempt.id,
            execution_generation=retried.attempt.execution_generation,
        )

        assert second_execution.status == "ready"
        assert plugin.convert_calls == 2
        assert retried.attempt.status == "completed"
        assert retried.attempt.transport_retry_count == 1
        retry_audit = session.scalar(
            select(AuditEvent).where(
                AuditEvent.action == "library.conversion.retried",
                AuditEvent.target_id == retried.attempt.id,
            )
        )
        assert retry_audit is not None
        assert retry_audit.retention_class == "sensitive"
        assert retry_audit.payload is not None
        assert (
            retry_audit.payload["previous_failure"]["transport_retry_count"]
            == max_transport_retries
        )


def test_manual_conversion_retry_locks_job_before_domain_lineage(
    session_factory,
    tmp_path: Path,
    limits: ConversionLimits,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plugin = _ScriptedConverter(
        "test.manual-retry-lock-order",
        priority=100,
        outcomes=(ConversionResult(markdown="# retry"),),
    )
    orchestrator, store = _orchestrator(tmp_path, plugin)
    with session_factory.begin() as session:
        workspace, item, version = _persist_source(session, store)
        scheduled = _schedule(orchestrator, session, workspace, version, limits)
        assert scheduled.attempt is not None
        assert scheduled.job is not None
        orchestrator.mark_terminal_failure(
            session=session,
            workspace_id=workspace.id,
            attempt_id=scheduled.attempt.id,
            execution_generation=scheduled.attempt.execution_generation,
            manual_retryable=True,
            safe_error_code="converter_process_failed",
        )
        orchestrator.job_queue.complete(session=session, job=scheduled.job)
        workspace_id = workspace.id
        item_id = item.id
        version_id = version.id

    locked_tables: list[str] = []
    real_scalar = Session.scalar

    def record_locked_table(self, statement, *args, **kwargs):
        if getattr(statement, "_for_update_arg", None) is not None:
            entity = statement.column_descriptions[0].get("entity")
            locked_tables.append(entity.__tablename__)
        return real_scalar(self, statement, *args, **kwargs)

    monkeypatch.setattr(Session, "scalar", record_locked_table)
    with session_factory.begin() as session:
        orchestrator.retry_failed_conversion(
            session=session,
            workspace_id=workspace_id,
            library_item_id=item_id,
            version_id=version_id,
            actor_kind="human",
            actor_id=str(uuid4()),
        )

    assert locked_tables == [
        "job",
        "library_item",
        "library_item_version",
        "library_run",
        "library_attempt",
    ]


def test_schedule_initial_is_persisted_and_idempotent(
    session_factory,
    tmp_path: Path,
    limits: ConversionLimits,
) -> None:
    plugin = _ScriptedConverter(
        "test.primary",
        priority=100,
        outcomes=(ConversionResult(markdown="# Ready"),),
    )
    orchestrator, store = _orchestrator(tmp_path, plugin)

    with session_factory.begin() as session:
        workspace, _, version = _persist_source(session, store)
        first = _schedule(orchestrator, session, workspace, version, limits)
        second = _schedule(orchestrator, session, workspace, version, limits)

        assert first.run.id == second.run.id
        assert first.attempt is not None
        assert second.attempt is not None
        assert first.attempt.id == second.attempt.id
        assert first.job is not None
        assert second.job is not None
        assert first.job.id == second.job.id
        assert first.run.status == "queued"
        assert version.processing_status == "queued"
        assert first.run.provider_chain_json[0]["converter_id"] == "test.primary"
        assert first.job.target_ref["execution_generation"] == 1
        assert session.scalar(select(func.count(LibraryRun.id))) == 1
        assert session.scalar(select(func.count(LibraryAttempt.id))) == 1
        assert (
            session.scalar(
                select(func.count(Job.id)).where(
                    Job.job_type == LIBRARY_CONVERSION_JOB_TYPE
                )
            )
            == 1
        )


def test_schedule_without_provider_preserves_source_as_stored_unconverted(
    session_factory,
    tmp_path: Path,
    limits: ConversionLimits,
) -> None:
    orchestrator, store = _orchestrator(tmp_path)

    with session_factory.begin() as session:
        workspace, item, version = _persist_source(session, store)
        scheduled = _schedule(orchestrator, session, workspace, version, limits)

        assert scheduled.run.status == "stored_unconverted"
        assert scheduled.run.finished_at is not None
        assert scheduled.attempt is None
        assert scheduled.job is None
        assert version.processing_status == "stored_unconverted"
        assert item.current_ready_version_id is None
        assert session.scalar(select(func.count(LibraryAttempt.id))) == 0
        assert (
            session.scalar(
                select(func.count(Job.id)).where(
                    Job.job_type == LIBRARY_CONVERSION_JOB_TYPE
                )
            )
            == 0
        )


def test_provider_input_safety_cap_is_pinned_in_execution_recipe(
    session_factory,
    tmp_path: Path,
    limits: ConversionLimits,
) -> None:
    plugin = _ScriptedConverter(
        "test.bounded-provider",
        priority=100,
        outcomes=(ConversionResult(markdown="# Bounded"),),
        max_input_bytes=512,
    )
    orchestrator, store = _orchestrator(tmp_path, plugin)

    with session_factory.begin() as session:
        workspace, _, version = _persist_source(session, store)
        scheduled = _schedule(orchestrator, session, workspace, version, limits)

        assert scheduled.attempt is not None
        assert scheduled.run.provider_chain_json[0]["max_input_bytes"] == 512
        execution = orchestrator.execute_attempt(
            session=session,
            workspace_id=workspace.id,
            attempt_id=scheduled.attempt.id,
            execution_generation=1,
        )

        assert execution.status == "ready"
        assert plugin.convert_calls == 1


def test_schedule_skips_unhealthy_higher_priority_provider(
    session_factory,
    tmp_path: Path,
    limits: ConversionLimits,
) -> None:
    unhealthy = _HealthScriptedConverter(
        "test.unhealthy",
        priority=200,
        outcomes=(ConversionResult(markdown="# Must not run"),),
        ready=False,
    )
    healthy = _HealthScriptedConverter(
        "test.healthy",
        priority=100,
        outcomes=(ConversionResult(markdown="# Ready"),),
        ready=True,
    )
    orchestrator, store = _orchestrator(tmp_path, unhealthy, healthy)

    with session_factory.begin() as session:
        workspace, _, version = _persist_source(session, store)
        scheduled = _schedule(orchestrator, session, workspace, version, limits)

        assert scheduled.run.status == "queued"
        assert scheduled.run.provider_chain_json[0]["converter_id"] == "test.healthy"
        assert len(scheduled.run.provider_chain_json) == 1
        assert scheduled.attempt is not None
        assert scheduled.attempt.provider_id == "test.healthy"
        execution = orchestrator.execute_attempt(
            session=session,
            workspace_id=workspace.id,
            attempt_id=scheduled.attempt.id,
            execution_generation=1,
        )

        assert execution.status == "ready"
        assert execution.selected is True
        assert unhealthy.healthcheck_calls == 1
        assert healthy.healthcheck_calls == 1
        assert unhealthy.probe_calls == 0
        assert unhealthy.convert_calls == 0
        assert healthy.probe_calls == 1
        assert healthy.convert_calls == 1


def test_schedule_with_only_unhealthy_provider_is_stored_unconverted(
    session_factory,
    tmp_path: Path,
    limits: ConversionLimits,
) -> None:
    unhealthy = _HealthScriptedConverter(
        "test.unhealthy-only",
        priority=200,
        outcomes=(ConversionResult(markdown="# Must not run"),),
        ready=False,
    )
    orchestrator, store = _orchestrator(tmp_path, unhealthy)

    with session_factory.begin() as session:
        workspace, item, version = _persist_source(session, store)
        scheduled = _schedule(orchestrator, session, workspace, version, limits)

        assert scheduled.run.status == "stored_unconverted"
        assert scheduled.run.provider_chain_json == []
        assert scheduled.attempt is None
        assert scheduled.job is None
        assert version.processing_status == "stored_unconverted"
        assert item.current_ready_version_id is None
        assert unhealthy.healthcheck_calls == 1
        assert unhealthy.probe_calls == 0
        assert unhealthy.convert_calls == 0


def test_execute_attempt_cross_workspace_is_opaque_not_found_without_mutation(
    session_factory,
    tmp_path: Path,
    limits: ConversionLimits,
) -> None:
    plugin = _ScriptedConverter(
        "test.cross-workspace-execute",
        priority=100,
        outcomes=(ConversionResult(markdown="# Must not run"),),
    )
    orchestrator, store = _orchestrator(tmp_path, plugin)

    with session_factory.begin() as session:
        source_workspace, item, version = _persist_source(session, store)
        scheduled = _schedule(
            orchestrator,
            session,
            source_workspace,
            version,
            limits,
        )
        assert scheduled.attempt is not None
        foreign_workspace = Workspace(
            slug="foreign-conversion-execute",
            title="Foreign Conversion Execute",
            name="Foreign Conversion Execute",
        )
        session.add(foreign_workspace)
        session.flush()

        with pytest.raises(ValueError) as missing_error:
            orchestrator.execute_attempt(
                session=session,
                workspace_id=foreign_workspace.id,
                attempt_id=uuid4(),
                execution_generation=1,
            )
        with pytest.raises(ValueError) as foreign_error:
            orchestrator.execute_attempt(
                session=session,
                workspace_id=foreign_workspace.id,
                attempt_id=scheduled.attempt.id,
                execution_generation=1,
            )

        conversion_audits = session.scalars(
            select(AuditEvent.id).where(
                AuditEvent.action.in_(
                    {"library.conversion.completed", "library.conversion.failed"}
                )
            )
        ).all()
        artifacts = session.scalars(select(LibraryArtifact.id)).all()

    assert str(foreign_error.value) == str(missing_error.value)
    assert str(source_workspace.id) not in str(foreign_error.value)
    assert scheduled.attempt.status == "queued"
    assert scheduled.run.status == "queued"
    assert version.processing_status == "queued"
    assert item.current_ready_version_id is None
    assert conversion_audits == []
    assert artifacts == []
    assert plugin.probe_calls == 0
    assert plugin.convert_calls == 0


def test_worker_rejects_cross_workspace_conversion_job_without_lineage_mutation(
    session_factory,
    tmp_path: Path,
    limits: ConversionLimits,
) -> None:
    plugin = _ScriptedConverter(
        "test.cross-workspace-worker",
        priority=100,
        outcomes=(ConversionResult(markdown="# Must not run"),),
    )
    orchestrator, store = _orchestrator(tmp_path, plugin)

    with session_factory.begin() as session:
        source_workspace, _, version = _persist_source(session, store)
        scheduled = _schedule(
            orchestrator,
            session,
            source_workspace,
            version,
            limits,
        )
        assert scheduled.attempt is not None
        assert scheduled.job is not None
        foreign_workspace = Workspace(
            slug="foreign-conversion-worker",
            title="Foreign Conversion Worker",
            name="Foreign Conversion Worker",
        )
        session.add(foreign_workspace)
        session.flush()
        foreign_job = orchestrator.job_queue.enqueue(
            session=session,
            workspace_id=foreign_workspace.id,
            job_type=LIBRARY_CONVERSION_JOB_TYPE,
            trigger_type="cross_workspace_test",
            idempotency_key=(
                f"cross-workspace:{scheduled.attempt.id}:"
                f"{scheduled.attempt.execution_generation}"
            ),
            target_ref={
                "library_conversion_attempt_id": str(scheduled.attempt.id),
                "library_conversion_run_id": str(scheduled.run.id),
                "library_item_version_id": str(version.id),
                "execution_generation": scheduled.attempt.execution_generation,
            },
        )
        source_workspace_id = source_workspace.id
        foreign_workspace_id = foreign_workspace.id
        source_job_id = scheduled.job.id
        foreign_job_id = foreign_job.id
        attempt_id = scheduled.attempt.id
        run_id = scheduled.run.id
        version_id = version.id

    worker = OneShotWorker(job_queue=orchestrator.job_queue)
    worker.library_conversion_service = orchestrator
    dispatch = worker.dispatch_once_with_session_factory(
        session_factory,
        workspace_id=foreign_workspace_id,
        job_types={LIBRARY_CONVERSION_JOB_TYPE},
    )

    assert dispatch is not None
    assert dispatch.job_id == foreign_job_id
    assert dispatch.status == "failed"
    assert dispatch.detail == {
        "error": "Library conversion attempt was not found",
        "terminal": True,
    }
    assert str(source_workspace_id) not in dispatch.detail["error"]
    assert plugin.probe_calls == 0
    assert plugin.convert_calls == 0

    with session_factory() as session:
        source_job = session.get(Job, source_job_id)
        persisted_foreign_job = session.get(Job, foreign_job_id)
        attempt = session.get(LibraryAttempt, attempt_id)
        run = session.get(LibraryRun, run_id)
        persisted_version = session.get(LibraryItemVersion, version_id)
        conversion_audits = session.scalars(
            select(AuditEvent.id).where(
                AuditEvent.action.in_(
                    {"library.conversion.completed", "library.conversion.failed"}
                )
            )
        ).all()

    assert source_job is not None and source_job.status == "queued"
    assert persisted_foreign_job is not None
    assert persisted_foreign_job.status == "failed"
    assert persisted_foreign_job.retry_count == 1
    assert attempt is not None and attempt.status == "queued"
    assert run is not None and run.status == "queued"
    assert persisted_version is not None
    assert persisted_version.processing_status == "queued"
    assert conversion_audits == []


def test_success_promotes_canonical_artifact_and_duplicate_execution_is_noop(
    session_factory,
    tmp_path: Path,
    limits: ConversionLimits,
) -> None:
    plugin = _ScriptedConverter(
        "test.success",
        priority=100,
        outcomes=(ConversionResult(markdown="# Converted\n\nBody"),),
    )
    orchestrator, store = _orchestrator(tmp_path, plugin)

    with session_factory.begin() as session:
        workspace, item, version = _persist_source(session, store)
        scheduled = _schedule(orchestrator, session, workspace, version, limits)
        assert scheduled.attempt is not None

        result = orchestrator.execute_attempt(
            session=session,
            workspace_id=workspace.id,
            attempt_id=scheduled.attempt.id,
            execution_generation=1,
        )
        duplicate = orchestrator.execute_attempt(
            session=session,
            workspace_id=workspace.id,
            attempt_id=scheduled.attempt.id,
            execution_generation=1,
        )
        orchestrator.mark_terminal_failure(
            session=session,
            workspace_id=workspace.id,
            attempt_id=scheduled.attempt.id,
            execution_generation=1,
            manual_retryable=True,
        )

        assert result.status == "ready"
        assert result.selected is True
        assert duplicate.status == "ready"
        assert duplicate.selected is True
        assert scheduled.attempt.status == "completed"
        assert plugin.convert_calls == 1
        assert version.selected_conversion_attempt_id == scheduled.attempt.id
        assert version.processing_status == "ready"
        assert item.current_ready_version_id == version.id
        assert item.lifecycle_status == "registered"
        artifact = session.scalar(select(LibraryArtifact))
        assert artifact is not None
        assert artifact.attempt_id == scheduled.attempt.id
        assert artifact.role == "markdown"
        assert artifact.blob_id is not None
        assert artifact.byte_size is not None
        artifact_blob = session.get(
            BlobIncarnationRow,
            {
                "workspace_id": artifact.blob_workspace_id,
                "blob_id": artifact.blob_id,
                "blob_epoch": artifact.blob_epoch,
            },
        )
        assert artifact_blob is not None
        assert artifact_blob.state == "available"
        assert artifact_blob.storage_key.startswith("b/")
        install_request = session.scalar(
            select(MediaInstallRequest).where(
                MediaInstallRequest.workspace_id == workspace.id,
                MediaInstallRequest.blob_id == artifact.blob_id,
            )
        )
        assert install_request is not None
        assert install_request.idempotency_key == (
            f"library-conversion:{scheduled.attempt.id}:markdown:0"
        )
        assert install_request.state == "available"
        inspection = session.scalar(
            select(StoredBlobInspectionRow).where(
                StoredBlobInspectionRow.workspace_id == workspace.id,
                StoredBlobInspectionRow.blob_id == artifact.blob_id,
                StoredBlobInspectionRow.blob_epoch == artifact.blob_epoch,
            )
        )
        assert inspection is not None
        assert inspection.detector_id == "library-conversion-output"
        assert inspection.metadata_json["converter_id"] == "test.success"
        charge = session.scalar(
            select(MediaPhysicalCharge).where(
                MediaPhysicalCharge.workspace_id == workspace.id,
                MediaPhysicalCharge.blob_id == artifact.blob_id,
                MediaPhysicalCharge.blob_epoch == artifact.blob_epoch,
                MediaPhysicalCharge.state == "charged",
            )
        )
        assert charge is not None
        assert charge.accounting_kind == "available"
        assert charge.storage_key == artifact_blob.storage_key
        logical_usage = session.get(
            LibraryLogicalQuotaUsage,
            {"workspace_id": workspace.id, "quota_kind": "derived"},
        )
        logical_charge = session.scalar(
            select(LibraryLogicalCharge).where(
                LibraryLogicalCharge.workspace_id == workspace.id,
                LibraryLogicalCharge.quota_kind == "derived",
                LibraryLogicalCharge.association_id == artifact.id,
            )
        )
        assert logical_usage is not None
        assert logical_usage.charged_bytes == artifact.byte_size
        assert logical_charge is not None
        assert logical_charge.byte_size == artifact.byte_size
        assert logical_charge.state == "charged"
        outbox_topics = set(
            session.scalars(
                select(MediaOutboxRow.topic).where(
                    MediaOutboxRow.workspace_id == workspace.id,
                    MediaOutboxRow.blob_id == artifact.blob_id,
                    MediaOutboxRow.blob_epoch == artifact.blob_epoch,
                )
            )
        )
        assert outbox_topics == {"InstallRequested", "BlobAvailable"}
        with store.open_handle(
            artifact_blob.storage_key,
            byte_size=artifact.byte_size,
        ).open_binary() as stream:
            assert stream.read() == b"# Converted\n\nBody"
        assert session.scalar(select(func.count(LibraryArtifact.id))) == 1
        assert (
            session.scalar(
                select(func.count(MediaInstallRequest.id)).where(
                    MediaInstallRequest.workspace_id == workspace.id,
                    MediaInstallRequest.blob_id == artifact.blob_id,
                )
            )
            == 1
        )
        assert session.scalar(select(func.count(LibraryProjectionOutbox.id))) == 1
        assert (
            session.scalar(select(func.count(Job.id)).where(Job.job_type == "embed"))
            == 1
        )
        assert (
            session.scalar(
                select(func.count(AuditEvent.id)).where(
                    AuditEvent.action == "library.conversion.completed"
                )
            )
            == 1
        )


def test_derived_quota_rejection_does_not_install_or_associate_output(
    session_factory,
    tmp_path: Path,
    limits: ConversionLimits,
) -> None:
    plugin = _ScriptedConverter(
        "test.quota",
        priority=100,
        outcomes=(ConversionResult(markdown="# Rejected by quota"),),
    )
    orchestrator, store = _orchestrator(tmp_path, plugin)

    with session_factory.begin() as session:
        workspace, _, version = _persist_source(session, store)
        session.add(
            LibraryLogicalQuotaUsage(
                workspace_id=workspace.id,
                quota_kind="derived",
                limit_bytes=0,
                charged_bytes=0,
                revision=0,
            )
        )
        session.flush()
        scheduled = _schedule(orchestrator, session, workspace, version, limits)
        assert scheduled.attempt is not None

        result = orchestrator.execute_attempt(
            session=session,
            workspace_id=workspace.id,
            attempt_id=scheduled.attempt.id,
            execution_generation=1,
        )

        assert result.status == "failed"
        assert scheduled.attempt.error_code == "policy_rejected"
        assert session.scalar(select(func.count(LibraryArtifact.id))) == 0
        assert session.scalar(select(func.count(LibraryLogicalCharge.id))) == 0
        assert session.scalar(select(func.count(MediaInstallRequest.id))) == 0
        assert session.scalar(select(func.count(StoredBlobRow.id))) == 1


def test_durable_duplicate_digest_uses_first_batch_canonical_media_type(
    session_factory,
    tmp_path: Path,
    limits: ConversionLimits,
) -> None:
    shared_content = b'{"same":true}'
    plugin = _ScriptedConverter(
        "test.durable-duplicate-digest",
        priority=100,
        outcomes=(
            ConversionResult(
                markdown=None,
                assets=(
                    ProducedAsset(
                        role="asset",
                        media_type="text/plain",
                        content=shared_content,
                        ordinal=0,
                    ),
                    ProducedAsset(
                        role="metadata",
                        media_type="application/json",
                        content=shared_content,
                        ordinal=1,
                    ),
                ),
            ),
        ),
    )
    orchestrator, store = _orchestrator(tmp_path, plugin)
    orchestrator.durable_media_installer_factory = (
        lambda _session_factory, _workspace_id: _durable_media_installer(
            store, session_factory
        )
    )

    with session_factory.begin() as session:
        workspace, _, version = _persist_source(session, store)
        scheduled = _schedule(orchestrator, session, workspace, version, limits)
        assert scheduled.attempt is not None
        workspace_id = workspace.id
        attempt_id = scheduled.attempt.id

    result = orchestrator.execute_attempt_durable(
        session_factory=session_factory,
        workspace_id=workspace_id,
        attempt_id=attempt_id,
        execution_generation=1,
    )

    assert result.status == "ready"
    with session_factory() as session:
        artifacts = session.scalars(
            select(LibraryArtifact)
            .where(
                LibraryArtifact.attempt_id == attempt_id,
                LibraryArtifact.stage == "conversion",
            )
            .order_by(LibraryArtifact.ordinal)
        ).all()
        assert len(artifacts) == 2
        assert [artifact.media_type for artifact in artifacts] == [
            "text/plain",
            "application/json",
        ]
        assert artifacts[0].content_sha256 == artifacts[1].content_sha256
        assert artifacts[0].blob_id == artifacts[1].blob_id
        assert artifacts[0].blob_epoch == artifacts[1].blob_epoch
        inspection = session.scalar(
            select(StoredBlobInspectionRow).where(
                StoredBlobInspectionRow.workspace_id == workspace_id,
                StoredBlobInspectionRow.blob_id == artifacts[0].blob_id,
                StoredBlobInspectionRow.blob_epoch == artifacts[0].blob_epoch,
            )
        )
        assert inspection is not None
        assert inspection.canonical_media_type == "text/plain"
        assert session.scalar(select(func.count(MediaInstallRequest.id))) == 1


def test_durable_conversion_quota_race_leaves_tracked_physical_orphan(
    session_factory,
    tmp_path: Path,
    limits: ConversionLimits,
) -> None:
    plugin = _ScriptedConverter(
        "test.durable-quota-race",
        priority=100,
        outcomes=(ConversionResult(markdown="# Installed before quota race"),),
    )
    orchestrator, store = _orchestrator(tmp_path, plugin)

    with session_factory.begin() as session:
        workspace, _, version = _persist_source(session, store)
        scheduled = _schedule(orchestrator, session, workspace, version, limits)
        assert scheduled.attempt is not None
        workspace_id = workspace.id
        attempt_id = scheduled.attempt.id

    inner = _durable_media_installer(store, session_factory)

    class _TightenQuotaAfterInstall:
        def execute(self, **kwargs):
            available = inner.execute(**kwargs)
            with session_factory.begin() as session:
                usage = session.get(
                    LibraryLogicalQuotaUsage,
                    {"workspace_id": workspace_id, "quota_kind": "derived"},
                )
                assert usage is not None
                usage.limit_bytes = usage.charged_bytes
            return available

    orchestrator.durable_media_installer_factory = lambda _session, _workspace_id: (
        _TightenQuotaAfterInstall()
    )
    result = orchestrator.execute_attempt_durable(
        session_factory=session_factory,
        workspace_id=workspace_id,
        attempt_id=attempt_id,
        execution_generation=1,
    )

    assert result.status == "failed"
    with session_factory() as session:
        attempt = session.get(LibraryAttempt, attempt_id)
        request = session.scalar(select(MediaInstallRequest))
        assert request is not None
        incarnation = session.get(
            BlobIncarnationRow,
            {
                "workspace_id": request.workspace_id,
                "blob_id": request.blob_id,
                "blob_epoch": request.blob_epoch,
            },
        )
        available_charge = session.scalar(
            select(MediaPhysicalCharge).where(
                MediaPhysicalCharge.accounting_kind == "available",
                MediaPhysicalCharge.blob_id == request.blob_id,
            )
        )
        assert attempt is not None and attempt.error_code == "policy_rejected"
        assert session.scalar(select(func.count(LibraryArtifact.id))) == 0
        assert session.scalar(select(func.count(LibraryLogicalCharge.id))) == 0
        assert session.scalar(select(func.count(StoredBlobRow.id))) == 2
        assert request.state == "available"
        assert incarnation is not None
        assert available_charge is not None and available_charge.state == "charged"
        assert store.path_for_key(incarnation.storage_key).is_file()


def test_durable_recipe_error_persists_failed_state_before_converter_execution(
    session_factory,
    tmp_path: Path,
    limits: ConversionLimits,
) -> None:
    plugin = _ScriptedConverter(
        "test.durable-recipe",
        priority=100,
        outcomes=(ConversionResult(markdown="must not run"),),
    )
    orchestrator, store = _orchestrator(tmp_path, plugin)
    orchestrator.durable_media_installer_factory = (
        lambda _session_factory, _workspace_id: _durable_media_installer(
            store, session_factory
        )
    )

    with session_factory.begin() as session:
        workspace, _, version = _persist_source(session, store)
        scheduled = _schedule(orchestrator, session, workspace, version, limits)
        assert scheduled.attempt is not None
        scheduled.attempt.config_hash = "b" * 64
        workspace_id = workspace.id
        version_id = version.id
        run_id = scheduled.run.id
        attempt_id = scheduled.attempt.id

    result = orchestrator.execute_attempt_durable(
        session_factory=session_factory,
        workspace_id=workspace_id,
        attempt_id=attempt_id,
        execution_generation=1,
    )

    assert result.status == "failed"
    assert result.selected is False
    assert plugin.probe_calls == 0
    assert plugin.convert_calls == 0
    with session_factory() as session:
        attempt = session.get(LibraryAttempt, attempt_id)
        run = session.get(LibraryRun, run_id)
        version = session.get(LibraryItemVersion, version_id)
        assert attempt is not None
        assert attempt.status == "failed"
        assert attempt.error_code == "converter_recipe_invalid"
        assert run is not None and run.status == "failed"
        assert version is not None and version.processing_status == "failed"
        assert session.scalar(select(func.count(LibraryArtifact.id))) == 0
        assert session.scalar(select(func.count(MediaInstallRequest.id))) == 0


def test_durable_total_budget_timeout_persists_timed_out_state_before_conversion(
    session_factory,
    tmp_path: Path,
    limits: ConversionLimits,
) -> None:
    plugin = _ScriptedConverter(
        "test.durable-total-timeout",
        priority=100,
        outcomes=(ConversionResult(markdown="must not run"),),
    )
    orchestrator, store = _orchestrator(tmp_path, plugin)
    orchestrator.durable_media_installer_factory = (
        lambda _session_factory, _workspace_id: _durable_media_installer(
            store, session_factory
        )
    )

    with session_factory.begin() as session:
        workspace, _, version = _persist_source(session, store)
        scheduled = _schedule(orchestrator, session, workspace, version, limits)
        assert scheduled.attempt is not None
        total_budget = dict(scheduled.run.conversion_detail.total_budget_json)
        total_budget["max_wall_seconds"] = 0.001
        scheduled.run.conversion_detail.total_budget_json = total_budget
        scheduled.run.created_at = utc_now() - timedelta(seconds=1)
        workspace_id = workspace.id
        version_id = version.id
        run_id = scheduled.run.id
        attempt_id = scheduled.attempt.id

    result = orchestrator.execute_attempt_durable(
        session_factory=session_factory,
        workspace_id=workspace_id,
        attempt_id=attempt_id,
        execution_generation=1,
    )

    assert result.status == "timed_out"
    assert result.selected is False
    assert plugin.probe_calls == 0
    assert plugin.convert_calls == 0
    with session_factory() as session:
        attempt = session.get(LibraryAttempt, attempt_id)
        run = session.get(LibraryRun, run_id)
        version = session.get(LibraryItemVersion, version_id)
        assert attempt is not None
        assert attempt.status == "timed_out"
        assert attempt.error_code == "conversion_total_timeout"
        assert run is not None and run.status == "timed_out"
        assert version is not None and version.processing_status == "timed_out"
        assert session.scalar(select(func.count(LibraryArtifact.id))) == 0
        assert session.scalar(select(func.count(MediaInstallRequest.id))) == 0


@pytest.mark.parametrize(
    ("snapshot_kind", "expected_code"),
    [
        ("provider_chain", "conversion_provider_snapshot_invalid"),
        ("provider_input_cap", "conversion_provider_snapshot_invalid"),
        ("limits", "conversion_limit_snapshot_invalid"),
        ("total_budget", "conversion_total_budget_invalid"),
        ("retry_budget", "conversion_retry_snapshot_invalid"),
        ("fallback", "conversion_fallback_snapshot_invalid"),
    ],
)
def test_durable_malformed_snapshot_fails_closed_before_converter_and_install(
    session_factory,
    tmp_path: Path,
    limits: ConversionLimits,
    snapshot_kind: str,
    expected_code: str,
) -> None:
    plugin = _ScriptedConverter(
        f"test.durable-malformed-{snapshot_kind}",
        priority=100,
        outcomes=(ConversionResult(markdown="must not run"),),
    )
    orchestrator, store = _orchestrator(tmp_path, plugin)
    installer_factory_called = False

    def unexpected_installer_factory(_session_factory, _workspace_id):
        nonlocal installer_factory_called
        installer_factory_called = True
        raise AssertionError("malformed durable snapshot reached installer creation")

    orchestrator.durable_media_installer_factory = unexpected_installer_factory

    with session_factory.begin() as session:
        workspace, _, version = _persist_source(session, store)
        scheduled = _schedule(orchestrator, session, workspace, version, limits)
        assert scheduled.attempt is not None
        if snapshot_kind == "provider_chain":
            scheduled.run.provider_chain_json = [
                {"converter_id": plugin.descriptor().converter_id}
            ]
        elif snapshot_kind == "provider_input_cap":
            malformed_provider = dict(scheduled.run.provider_chain_json[0])
            malformed_provider["max_input_bytes"] = True
            scheduled.run.provider_chain_json = [malformed_provider]
        elif snapshot_kind == "limits":
            budget = dict(scheduled.run.conversion_detail.per_provider_budget_json)
            budget["limits"] = {"max_input_bytes": 0}
            scheduled.run.conversion_detail.per_provider_budget_json = budget
        elif snapshot_kind == "total_budget":
            scheduled.run.conversion_detail.total_budget_json = {
                "max_provider_attempts": "one",
                "max_wall_seconds": 5,
            }
        elif snapshot_kind == "retry_budget":
            budget = dict(scheduled.run.conversion_detail.per_provider_budget_json)
            budget["max_transport_retries"] = "one"
            scheduled.run.conversion_detail.per_provider_budget_json = budget
        else:
            scheduled.run.conversion_detail.error_fallback_matrix_json = {
                "default": "unbounded-action"
            }
        workspace_id = workspace.id
        version_id = version.id
        run_id = scheduled.run.id
        attempt_id = scheduled.attempt.id

    result = orchestrator.execute_attempt_durable(
        session_factory=session_factory,
        workspace_id=workspace_id,
        attempt_id=attempt_id,
        execution_generation=1,
    )

    assert result.status == "failed"
    assert result.selected is False
    assert plugin.probe_calls == 0
    assert plugin.convert_calls == 0
    assert installer_factory_called is False
    with session_factory() as session:
        attempt = session.get(LibraryAttempt, attempt_id)
        run = session.get(LibraryRun, run_id)
        version = session.get(LibraryItemVersion, version_id)
        audit = session.scalar(
            select(AuditEvent).where(
                AuditEvent.action == "library.conversion.failed",
                AuditEvent.target_id == attempt_id,
            )
        )
        assert attempt is not None
        assert attempt.status == "failed"
        assert attempt.error_code == expected_code
        assert attempt.transport_retry_count == 0
        assert attempt.execution_generation == 1
        assert run is not None and run.status == "failed"
        assert version is not None and version.processing_status == "failed"
        assert audit is not None and audit.payload is not None
        assert audit.payload["error_code"] == expected_code
        assert session.scalar(select(func.count(LibraryAttempt.id))) == 1
        assert session.scalar(select(func.count(LibraryArtifact.id))) == 0
        assert session.scalar(select(func.count(MediaInstallRequest.id))) == 0


def test_worker_completes_corrupt_fallback_snapshot_without_generic_retry(
    session_factory,
    tmp_path: Path,
    limits: ConversionLimits,
) -> None:
    plugin = _ScriptedConverter(
        "test.worker-corrupt-fallback",
        priority=100,
        outcomes=(ConversionResult(markdown="must not run"),),
    )
    orchestrator, store = _orchestrator(tmp_path, plugin)
    installer_factory_called = False

    def unexpected_installer_factory(_session_factory, _workspace_id):
        nonlocal installer_factory_called
        installer_factory_called = True
        raise AssertionError("corrupt fallback snapshot reached installer creation")

    orchestrator.durable_media_installer_factory = unexpected_installer_factory
    with session_factory.begin() as session:
        workspace, _, version = _persist_source(session, store)
        scheduled = _schedule(orchestrator, session, workspace, version, limits)
        assert scheduled.attempt is not None
        assert scheduled.job is not None
        scheduled.run.conversion_detail.error_fallback_matrix_json = {
            "default": "unbounded-action"
        }
        workspace_id = workspace.id
        version_id = version.id
        run_id = scheduled.run.id
        attempt_id = scheduled.attempt.id
        job_id = scheduled.job.id

    worker = OneShotWorker(job_queue=orchestrator.job_queue)
    worker.library_conversion_service = orchestrator
    dispatch = worker.dispatch_once_with_session_factory(
        session_factory,
        workspace_id=workspace_id,
        job_types={LIBRARY_CONVERSION_JOB_TYPE},
    )

    assert dispatch is not None
    assert dispatch.status == "completed"
    assert dispatch.detail["conversion_status"] == "failed"
    assert plugin.probe_calls == 0
    assert plugin.convert_calls == 0
    assert installer_factory_called is False
    with session_factory() as session:
        attempt = session.get(LibraryAttempt, attempt_id)
        run = session.get(LibraryRun, run_id)
        version = session.get(LibraryItemVersion, version_id)
        job = session.get(Job, job_id)
        audit = session.scalar(
            select(AuditEvent).where(
                AuditEvent.action == "library.conversion.failed",
                AuditEvent.target_id == attempt_id,
            )
        )
        assert attempt is not None
        assert attempt.status == "failed"
        assert attempt.error_code == "conversion_fallback_snapshot_invalid"
        assert attempt.transport_retry_count == 0
        assert attempt.execution_generation == 1
        assert run is not None and run.status == "failed"
        assert version is not None and version.processing_status == "failed"
        assert job is not None
        assert job.status == "completed"
        assert job.retry_count == 0
        assert audit is not None and audit.payload is not None
        assert audit.payload["error_code"] == "conversion_fallback_snapshot_invalid"
        assert session.scalar(select(func.count(LibraryAttempt.id))) == 1
        assert session.scalar(select(func.count(MediaInstallRequest.id))) == 0


def test_durable_output_capacity_guard_runs_before_quota_and_install(
    session_factory,
    tmp_path: Path,
    limits: ConversionLimits,
) -> None:
    drifted_limits = replace(
        limits,
        max_assets=MAX_DURABLE_CONVERSION_OUTPUTS,
    )
    plugin = _ScriptedConverter(
        "test.durable-output-capacity",
        priority=100,
        outcomes=(
            ConversionResult(
                markdown="# Capacity overflow",
                assets=tuple(
                    ProducedAsset(
                        role="asset",
                        media_type="text/plain",
                        content=b"x",
                        ordinal=index,
                    )
                    for index in range(MAX_DURABLE_CONVERSION_OUTPUTS)
                ),
            ),
        ),
    )
    orchestrator, store = _orchestrator(tmp_path, plugin)
    quota_factory_called = False
    installer_factory_called = False

    def unexpected_quota_factory(_session):
        nonlocal quota_factory_called
        quota_factory_called = True
        raise AssertionError("durable output overflow reached quota preflight")

    def unexpected_installer_factory(_session_factory, _workspace_id):
        nonlocal installer_factory_called
        installer_factory_called = True
        raise AssertionError("durable output overflow reached installer creation")

    orchestrator.logical_quota_factory = unexpected_quota_factory
    orchestrator.durable_media_installer_factory = unexpected_installer_factory

    with session_factory.begin() as session:
        workspace, _, version = _persist_source(session, store)
        scheduled = _schedule(
            orchestrator,
            session,
            workspace,
            version,
            drifted_limits,
        )
        assert scheduled.attempt is not None
        workspace_id = workspace.id
        version_id = version.id
        run_id = scheduled.run.id
        attempt_id = scheduled.attempt.id

    result = orchestrator.execute_attempt_durable(
        session_factory=session_factory,
        workspace_id=workspace_id,
        attempt_id=attempt_id,
        execution_generation=1,
    )

    assert result.status == "failed"
    assert plugin.probe_calls == 1
    assert plugin.convert_calls == 1
    assert quota_factory_called is False
    assert installer_factory_called is False
    with session_factory() as session:
        attempt = session.get(LibraryAttempt, attempt_id)
        run = session.get(LibraryRun, run_id)
        version = session.get(LibraryItemVersion, version_id)
        assert attempt is not None
        assert attempt.status == "failed"
        assert attempt.error_code == "conversion_output_capacity_exceeded"
        assert run is not None and run.status == "failed"
        assert version is not None and version.processing_status == "failed"
        assert session.scalar(select(func.count(LibraryArtifact.id))) == 0
        assert session.scalar(select(func.count(MediaInstallRequest.id))) == 0


def test_transport_retry_advances_fence_and_old_generation_becomes_stale(
    session_factory,
    tmp_path: Path,
    limits: ConversionLimits,
) -> None:
    plugin = _ScriptedConverter(
        "test.retry",
        priority=100,
        outcomes=(
            ConversionError(
                "transport_failure",
                "Temporary converter failure.",
                retryable=True,
                fallback_allowed=True,
            ),
            ConversionResult(markdown="# Recovered"),
        ),
    )
    orchestrator, store = _orchestrator(tmp_path, plugin)

    with session_factory.begin() as session:
        workspace, _, version = _persist_source(session, store)
        scheduled = _schedule(orchestrator, session, workspace, version, limits)
        assert scheduled.attempt is not None

        retry = orchestrator.execute_attempt(
            session=session,
            workspace_id=workspace.id,
            attempt_id=scheduled.attempt.id,
            execution_generation=1,
        )
        stale = orchestrator.execute_attempt(
            session=session,
            workspace_id=workspace.id,
            attempt_id=scheduled.attempt.id,
            execution_generation=1,
        )
        recovered = orchestrator.execute_attempt(
            session=session,
            workspace_id=workspace.id,
            attempt_id=scheduled.attempt.id,
            execution_generation=2,
        )

        assert retry.status == "requeued"
        assert stale.status == "stale"
        assert recovered.status == "ready"
        assert recovered.selected is True
        assert plugin.convert_calls == 2
        assert scheduled.attempt.transport_retry_count == 1
        assert scheduled.attempt.execution_generation == 2
        assert version.processing_status == "ready"
        conversion_jobs = session.scalars(
            select(Job)
            .where(Job.job_type == LIBRARY_CONVERSION_JOB_TYPE)
            .order_by(Job.created_at, Job.id)
        ).all()
        assert len(conversion_jobs) == 2
        assert {job.target_ref["execution_generation"] for job in conversion_jobs} == {
            1,
            2,
        }


def test_transport_retry_uses_snapshotted_budget_instead_of_process_default(
    session_factory,
    tmp_path: Path,
    limits: ConversionLimits,
) -> None:
    plugin = _ScriptedConverter(
        "test.no-retry",
        priority=100,
        outcomes=(
            ConversionError(
                "transport_failure",
                "Temporary converter failure.",
                retryable=True,
                fallback_allowed=True,
            ),
        ),
    )
    orchestrator, store = _orchestrator(tmp_path, plugin)

    with session_factory.begin() as session:
        workspace, _, version = _persist_source(session, store)
        scheduled = _schedule(orchestrator, session, workspace, version, limits)
        assert scheduled.attempt is not None
        budget = dict(scheduled.run.conversion_detail.per_provider_budget_json)
        budget["max_transport_retries"] = 0
        scheduled.run.conversion_detail.per_provider_budget_json = budget
        session.flush()

        result = orchestrator.execute_attempt(
            session=session,
            workspace_id=workspace.id,
            attempt_id=scheduled.attempt.id,
            execution_generation=1,
        )

        assert result.status == "failed"
        assert scheduled.attempt.status == "failed"
        assert scheduled.attempt.transport_retry_count == 0
        assert scheduled.attempt.execution_generation == 1
        assert version.processing_status == "failed"
        assert (
            session.scalar(
                select(func.count(Job.id)).where(
                    Job.job_type == LIBRARY_CONVERSION_JOB_TYPE
                )
            )
            == 1
        )


def test_exhausted_transport_budget_persists_allowed_manual_retry(
    session_factory,
    tmp_path: Path,
    limits: ConversionLimits,
) -> None:
    plugin = _ScriptedConverter(
        "test.exhausted-transport-budget",
        priority=100,
        outcomes=(
            ConversionError(
                "transport_failure",
                "Temporary converter failure.",
                retryable=True,
                fallback_allowed=False,
            ),
        ),
    )
    orchestrator, store = _orchestrator(tmp_path, plugin)

    with session_factory.begin() as session:
        workspace, _, version = _persist_source(session, store)
        scheduled = _schedule(orchestrator, session, workspace, version, limits)
        assert scheduled.attempt is not None
        budget_json = scheduled.run.conversion_detail.per_provider_budget_json
        max_transport_retries = budget_json["max_transport_retries"]
        assert isinstance(max_transport_retries, int)
        scheduled.attempt.transport_retry_count = max_transport_retries

        result = orchestrator.execute_attempt(
            session=session,
            workspace_id=workspace.id,
            attempt_id=scheduled.attempt.id,
            execution_generation=1,
        )

        assert result.status == "failed"
        assert scheduled.attempt.status == "failed"
        assert scheduled.attempt.error_code == "transport_failure"
        assert scheduled.attempt.conversion_detail.manual_retry_disposition == "allowed"
        assert (
            scheduled.attempt.conversion_detail.disposition_reason == "error_retryable"
        )
        assert version.processing_status == "failed"


def test_converter_timeout_claiming_retry_and_fallback_is_terminal_timed_out(
    session_factory,
    tmp_path: Path,
    limits: ConversionLimits,
) -> None:
    timed_out = _ScriptedConverter(
        "test.process-timeout",
        priority=200,
        outcomes=(
            ConversionError(
                "converter_timed_out",
                "Converter process exceeded its time budget.",
                retryable=True,
                fallback_allowed=True,
            ),
        ),
    )
    fallback = _ScriptedConverter(
        "test.timeout-fallback-must-not-run",
        priority=100,
        outcomes=(ConversionResult(markdown="must not run"),),
    )
    orchestrator, store = _orchestrator(tmp_path, timed_out, fallback)

    with session_factory.begin() as session:
        workspace, _, version = _persist_source(session, store)
        scheduled = _schedule(orchestrator, session, workspace, version, limits)
        assert scheduled.attempt is not None

        result = orchestrator.execute_attempt(
            session=session,
            workspace_id=workspace.id,
            attempt_id=scheduled.attempt.id,
            execution_generation=1,
        )

        assert (result.status, scheduled.attempt.error_code) == (
            "timed_out",
            "converter_timed_out",
        )
        assert scheduled.attempt.status == "timed_out"
        assert scheduled.attempt.error_code == "converter_timed_out"
        assert scheduled.attempt.conversion_detail.manual_retry_disposition == "denied"
        assert scheduled.attempt.conversion_detail.disposition_reason == "policy_denied"
        assert scheduled.attempt.transport_retry_count == 0
        assert scheduled.attempt.execution_generation == 1
        assert scheduled.run.status == "timed_out"
        assert version.processing_status == "timed_out"
        assert fallback.probe_calls == 0
        assert fallback.convert_calls == 0
        assert session.scalar(select(func.count(LibraryAttempt.id))) == 1


@pytest.mark.parametrize(
    "error_code",
    [
        "xml_entity_denied",
        "container_path_invalid",
        "active_content_denied",
        "invalid_ooxml",
    ],
)
def test_security_hard_stop_ignores_provider_retry_and_fallback_claims(
    session_factory,
    tmp_path: Path,
    limits: ConversionLimits,
    error_code: str,
) -> None:
    blocked = _ScriptedConverter(
        f"test.security-{error_code}",
        priority=200,
        outcomes=(
            ConversionError(
                error_code,
                "The converter rejected unsafe input.",
                retryable=True,
                fallback_allowed=True,
            ),
        ),
    )
    fallback = _ScriptedConverter(
        f"test.security-fallback-{error_code}",
        priority=100,
        outcomes=(ConversionResult(markdown="must not run"),),
    )
    orchestrator, store = _orchestrator(tmp_path, blocked, fallback)

    with session_factory.begin() as session:
        workspace, _, version = _persist_source(session, store)
        scheduled = _schedule(orchestrator, session, workspace, version, limits)
        assert scheduled.attempt is not None

        result = orchestrator.execute_attempt(
            session=session,
            workspace_id=workspace.id,
            attempt_id=scheduled.attempt.id,
            execution_generation=1,
        )

        assert result.status == "failed"
        assert result.next_attempt_id is None
        assert scheduled.attempt.status == "failed"
        assert scheduled.attempt.error_code == error_code
        assert scheduled.attempt.conversion_detail.manual_retry_disposition == "denied"
        assert scheduled.attempt.conversion_detail.disposition_reason == "policy_denied"
        assert scheduled.attempt.transport_retry_count == 0
        assert scheduled.attempt.execution_generation == 1
        assert scheduled.run.status == "failed"
        assert version.processing_status == "failed"
        assert fallback.probe_calls == 0
        assert fallback.convert_calls == 0
        assert session.scalar(select(func.count(LibraryAttempt.id))) == 1


def test_nonretryable_input_limit_persists_denied_manual_retry_disposition(
    session_factory,
    tmp_path: Path,
    limits: ConversionLimits,
) -> None:
    plugin = _ScriptedConverter(
        "test.input-limit",
        priority=100,
        outcomes=(
            ConversionError(
                "input_limit_exceeded",
                "The source exceeds the converter input limit.",
                retryable=False,
                fallback_allowed=False,
            ),
        ),
    )
    orchestrator, store = _orchestrator(tmp_path, plugin)

    with session_factory.begin() as session:
        workspace, item, version = _persist_source(session, store)
        scheduled = _schedule(orchestrator, session, workspace, version, limits)
        assert scheduled.attempt is not None
        assert scheduled.job is not None

        result = orchestrator.execute_attempt(
            session=session,
            workspace_id=workspace.id,
            attempt_id=scheduled.attempt.id,
            execution_generation=1,
        )

        assert result.status == "failed"
        assert scheduled.attempt.status == "failed"
        assert scheduled.attempt.error_code == "input_limit_exceeded"
        assert scheduled.attempt.conversion_detail.manual_retry_disposition == "denied"
        assert (
            scheduled.attempt.conversion_detail.disposition_reason
            == "error_not_retryable"
        )
        scheduled.job.status = "completed"

        with pytest.raises(LibraryConversionRetryNotAvailableError):
            orchestrator.retry_failed_conversion(
                session=session,
                workspace_id=workspace.id,
                library_item_id=item.id,
                version_id=version.id,
                actor_kind="human",
                actor_id=str(uuid4()),
            )

        assert scheduled.attempt.status == "failed"
        assert scheduled.attempt.execution_generation == 1
        assert scheduled.job.status == "completed"


def test_expired_total_wall_budget_returns_and_persists_timed_out(
    session_factory,
    tmp_path: Path,
    limits: ConversionLimits,
) -> None:
    plugin = _ScriptedConverter(
        "test.total-timeout",
        priority=100,
        outcomes=(ConversionResult(markdown="must not run"),),
    )
    orchestrator, store = _orchestrator(tmp_path, plugin)

    with session_factory.begin() as session:
        workspace, _, version = _persist_source(session, store)
        scheduled = _schedule(orchestrator, session, workspace, version, limits)
        assert scheduled.attempt is not None
        total_budget = dict(scheduled.run.conversion_detail.total_budget_json)
        total_budget["max_wall_seconds"] = 0.001
        scheduled.run.conversion_detail.total_budget_json = total_budget
        scheduled.run.created_at = utc_now() - timedelta(seconds=1)
        session.flush()

        result = orchestrator.execute_attempt(
            session=session,
            workspace_id=workspace.id,
            attempt_id=scheduled.attempt.id,
            execution_generation=1,
        )

        assert result.status == "timed_out"
        assert result.selected is False
        assert scheduled.attempt.status == "timed_out"
        assert scheduled.attempt.error_code == "conversion_total_timeout"
        assert scheduled.run.status == "timed_out"
        assert version.processing_status == "timed_out"
        assert plugin.probe_calls == 0
        assert plugin.convert_calls == 0


def test_stale_job_reclaim_advances_attempt_execution_fence(
    session_factory,
    tmp_path: Path,
    limits: ConversionLimits,
) -> None:
    plugin = _ScriptedConverter(
        "test.reclaim",
        priority=100,
        outcomes=(ConversionResult(markdown="# Reclaimed"),),
    )
    orchestrator, store = _orchestrator(tmp_path, plugin)

    with session_factory.begin() as session:
        workspace, _, version = _persist_source(session, store)
        scheduled = _schedule(orchestrator, session, workspace, version, limits)
        assert scheduled.attempt is not None
        assert scheduled.job is not None
        attempt_id = scheduled.attempt.id
        run_id = scheduled.run.id
        version_id = version.id
        job_id = scheduled.job.id
        scheduled.attempt.status = "running"
        scheduled.attempt.started_at = utc_now() - timedelta(seconds=30)
        scheduled.run.status = "running"
        version.processing_status = "converting"
        scheduled.job.status = "running"
        scheduled.job.started_at = utc_now() - timedelta(seconds=30)
        session.flush()

    worker = OneShotWorker()
    worker.library_conversion_service = orchestrator
    assert (
        worker.reclaim_stale_jobs_with_session_factory(
            session_factory,
            lease_timeout_seconds=1,
        )
        == 1
    )

    with session_factory.begin() as session:
        attempt = session.get(LibraryAttempt, attempt_id)
        run = session.get(LibraryRun, run_id)
        version = session.get(LibraryItemVersion, version_id)
        job = session.get(Job, job_id)
        assert attempt is not None
        assert run is not None
        assert version is not None
        assert job is not None
        assert attempt.status == "queued"
        assert attempt.execution_generation == 2
        assert attempt.transport_retry_count == 1
        assert run.status == "queued"
        assert version.processing_status == "queued"
        assert job.status == "queued"
        assert job.target_ref["execution_generation"] == 2
        assert job.idempotency_key == f"{attempt.id}:2"


def test_stale_job_reclaim_reconciles_terminal_attempt_without_resurrection(
    session_factory,
    tmp_path: Path,
    limits: ConversionLimits,
) -> None:
    plugin = _ScriptedConverter(
        "test.terminal-reclaim",
        priority=100,
        outcomes=(ConversionResult(markdown="must not run"),),
    )
    orchestrator, store = _orchestrator(tmp_path, plugin)

    with session_factory.begin() as session:
        workspace, _, version = _persist_source(session, store)
        scheduled = _schedule(orchestrator, session, workspace, version, limits)
        assert scheduled.attempt is not None
        assert scheduled.job is not None
        attempt_id = scheduled.attempt.id
        job_id = scheduled.job.id
        scheduled.attempt.status = "failed"
        scheduled.attempt.error_code = "invalid_format"
        scheduled.run.status = "failed"
        version.processing_status = "failed"
        scheduled.job.status = "running"
        scheduled.job.started_at = utc_now() - timedelta(seconds=30)
        session.flush()

    worker = OneShotWorker()
    worker.library_conversion_service = orchestrator
    assert (
        worker.reclaim_stale_jobs_with_session_factory(
            session_factory,
            lease_timeout_seconds=1,
        )
        == 1
    )

    with session_factory.begin() as session:
        attempt = session.get(LibraryAttempt, attempt_id)
        job = session.get(Job, job_id)
        assert attempt is not None
        assert job is not None
        assert attempt.status == "failed"
        assert attempt.execution_generation == 1
        assert attempt.transport_retry_count == 0
        assert job.status == "completed"
        assert job.progress == 100
        assert job.completed_at is not None
        assert job.next_attempt_at is None
        assert plugin.probe_calls == 0
        assert plugin.convert_calls == 0


def test_unsupported_provider_falls_back_to_next_snapshotted_provider(
    session_factory,
    tmp_path: Path,
    limits: ConversionLimits,
) -> None:
    unsupported = _ScriptedConverter(
        "test.unsupported",
        priority=200,
        outcomes=(ConversionResult(markdown="must not run"),),
        probe_supported=False,
    )
    fallback = _ScriptedConverter(
        "test.fallback",
        priority=100,
        outcomes=(ConversionResult(markdown="# Fallback"),),
    )
    orchestrator, store = _orchestrator(tmp_path, unsupported, fallback)

    with session_factory.begin() as session:
        workspace, _, version = _persist_source(session, store)
        scheduled = _schedule(orchestrator, session, workspace, version, limits)
        assert scheduled.attempt is not None

        first = orchestrator.execute_attempt(
            session=session,
            workspace_id=workspace.id,
            attempt_id=scheduled.attempt.id,
            execution_generation=1,
        )
        assert first.status == "fallback_queued"
        assert first.next_attempt_id is not None
        assert unsupported.convert_calls == 0
        next_attempt = session.get(
            LibraryAttempt,
            first.next_attempt_id,
        )
        assert next_attempt is not None
        assert next_attempt.parent_attempt_id == scheduled.attempt.id
        assert next_attempt.chain_index == 1
        assert next_attempt.provider_id == "test.fallback"

        second = orchestrator.execute_attempt(
            session=session,
            workspace_id=workspace.id,
            attempt_id=next_attempt.id,
            execution_generation=1,
        )
        assert second.status == "ready"
        assert second.selected is True
        assert fallback.convert_calls == 1
        assert version.selected_conversion_attempt_id == next_attempt.id


def test_snapshotted_stop_policy_overrides_malicious_provider_fallback_request(
    session_factory,
    tmp_path: Path,
    limits: ConversionLimits,
) -> None:
    malicious = _ScriptedConverter(
        "test.malicious",
        priority=200,
        outcomes=(
            ConversionError(
                "invalid_format",
                "The input is invalid.",
                retryable=True,
                fallback_allowed=True,
            ),
        ),
    )
    fallback = _ScriptedConverter(
        "test.must-not-run",
        priority=100,
        outcomes=(ConversionResult(markdown="must not run"),),
    )
    orchestrator, store = _orchestrator(tmp_path, malicious, fallback)

    with session_factory.begin() as session:
        workspace, item, version = _persist_source(session, store)
        scheduled = _schedule(orchestrator, session, workspace, version, limits)
        assert scheduled.attempt is not None

        result = orchestrator.execute_attempt(
            session=session,
            workspace_id=workspace.id,
            attempt_id=scheduled.attempt.id,
            execution_generation=1,
        )

        assert result.status == "failed"
        assert result.next_attempt_id is None
        assert scheduled.attempt.status == "failed"
        assert scheduled.attempt.transport_retry_count == 0
        assert version.processing_status == "failed"
        assert item.lifecycle_status == "registered"
        assert fallback.probe_calls == 0
        assert session.scalar(select(func.count(LibraryAttempt.id))) == 1


def test_late_success_preserves_archived_lifecycle_and_skips_embedding(
    session_factory,
    tmp_path: Path,
    limits: ConversionLimits,
) -> None:
    plugin = _ScriptedConverter(
        "test.archived",
        priority=100,
        outcomes=(ConversionResult(markdown="# Archived source"),),
    )
    orchestrator, store = _orchestrator(tmp_path, plugin)

    with session_factory.begin() as session:
        workspace, item, version = _persist_source(session, store)
        scheduled = _schedule(orchestrator, session, workspace, version, limits)
        assert scheduled.attempt is not None
        item.lifecycle_status = "archived"
        session.flush()

        result = orchestrator.execute_attempt(
            session=session,
            workspace_id=workspace.id,
            attempt_id=scheduled.attempt.id,
            execution_generation=1,
        )

        assert result.status == "ready"
        assert result.selected is True
        assert item.lifecycle_status == "archived"
        assert version.processing_status == "ready"
        assert item.current_ready_version_id == version.id
        assert (
            session.scalar(select(func.count(Job.id)).where(Job.job_type == "embed"))
            == 0
        )


def test_metadata_only_result_is_persisted_as_canonical_json_artifact(
    session_factory,
    tmp_path: Path,
    limits: ConversionLimits,
) -> None:
    plugin = _ScriptedConverter(
        "test.metadata",
        priority=100,
        outcomes=(
            ConversionResult(
                markdown=None,
                metadata={"title": "Metadata survives"},
                completeness="metadata_only",
            ),
        ),
    )
    orchestrator, store = _orchestrator(tmp_path, plugin)

    with session_factory.begin() as session:
        workspace, _, version = _persist_source(session, store)
        scheduled = _schedule(orchestrator, session, workspace, version, limits)
        assert scheduled.attempt is not None

        result = orchestrator.execute_attempt(
            session=session,
            workspace_id=workspace.id,
            attempt_id=scheduled.attempt.id,
            execution_generation=1,
        )

        assert result.status == "ready_partial"
        assert result.selected is True
        artifact = session.scalar(
            select(LibraryArtifact).where(
                LibraryArtifact.attempt_id == scheduled.attempt.id,
                LibraryArtifact.stage == "conversion",
            )
        )
        assert artifact is not None
        assert artifact.role == "metadata"
        assert artifact.media_type == "application/json"
        assert artifact.blob_id is not None
        artifact_blob = session.get(
            BlobIncarnationRow,
            {
                "workspace_id": artifact.blob_workspace_id,
                "blob_id": artifact.blob_id,
                "blob_epoch": artifact.blob_epoch,
            },
        )
        assert artifact_blob is not None
        with store.open_handle(
            artifact_blob.storage_key,
            byte_size=artifact_blob.byte_size,
        ).open_binary() as stream:
            payload = json.load(stream)
        assert payload["metadata"] == {"title": "Metadata survives"}
        assert payload["completeness"] == "metadata_only"
        assert version.selected_conversion_attempt_id == scheduled.attempt.id


def test_empty_complete_result_cannot_be_promoted_ready(
    session_factory,
    tmp_path: Path,
    limits: ConversionLimits,
) -> None:
    plugin = _ScriptedConverter(
        "test.empty",
        priority=100,
        outcomes=(ConversionResult(markdown=None),),
    )
    orchestrator, store = _orchestrator(tmp_path, plugin)

    with session_factory.begin() as session:
        workspace, item, version = _persist_source(session, store)
        scheduled = _schedule(orchestrator, session, workspace, version, limits)
        assert scheduled.attempt is not None

        result = orchestrator.execute_attempt(
            session=session,
            workspace_id=workspace.id,
            attempt_id=scheduled.attempt.id,
            execution_generation=1,
        )

        assert result.status == "failed"
        assert result.selected is False
        assert scheduled.attempt.error_code == "converter_result_empty"
        assert version.selected_conversion_attempt_id is None
        assert item.current_ready_version_id is None
        assert session.scalar(select(func.count(LibraryArtifact.id))) == 0


def test_metadata_with_provider_asset_does_not_expand_asset_budget(
    session_factory,
    tmp_path: Path,
    limits: ConversionLimits,
) -> None:
    asset_content = b'{"provider":true}'
    bounded_limits = replace(
        limits,
        max_assets=1,
        max_asset_bytes=len(asset_content),
    )
    plugin = _ScriptedConverter(
        "test.metadata-asset",
        priority=100,
        outcomes=(
            ConversionResult(
                markdown=None,
                assets=(
                    ProducedAsset(
                        role="metadata",
                        media_type="application/json",
                        content=asset_content,
                        filename="provider.json",
                    ),
                ),
                metadata={"preserved": True},
                completeness="metadata_only",
            ),
        ),
    )
    orchestrator, store = _orchestrator(tmp_path, plugin)

    with session_factory.begin() as session:
        workspace, _, version = _persist_source(session, store)
        scheduled = _schedule(
            orchestrator,
            session,
            workspace,
            version,
            bounded_limits,
        )
        assert scheduled.attempt is not None

        result = orchestrator.execute_attempt(
            session=session,
            workspace_id=workspace.id,
            attempt_id=scheduled.attempt.id,
            execution_generation=1,
        )

        assert result.status == "ready_partial"
        artifacts = session.scalars(
            select(LibraryArtifact).where(
                LibraryArtifact.attempt_id == scheduled.attempt.id,
                LibraryArtifact.stage == "conversion",
            )
        ).all()
        assert len(artifacts) == 1
        assert artifacts[0].byte_size == len(asset_content)
        assert scheduled.attempt.provenance_json["result_metadata"] == {
            "preserved": True
        }


def test_malformed_fallback_snapshot_fails_closed_without_next_attempt(
    session_factory,
    tmp_path: Path,
    limits: ConversionLimits,
) -> None:
    failing = _ScriptedConverter(
        "test.failure",
        priority=200,
        outcomes=(
            ConversionError(
                "unsupported",
                "Unsupported source.",
                fallback_allowed=True,
            ),
        ),
    )
    fallback = _ScriptedConverter(
        "test.hidden-fallback",
        priority=100,
        outcomes=(ConversionResult(markdown="must not run"),),
    )
    orchestrator, store = _orchestrator(tmp_path, failing, fallback)

    with session_factory.begin() as session:
        workspace, _, version = _persist_source(session, store)
        scheduled = _schedule(orchestrator, session, workspace, version, limits)
        assert scheduled.attempt is not None
        scheduled.run.conversion_detail.error_fallback_matrix_json = {
            "default": "unbounded-action"
        }
        session.flush()

        with pytest.raises(
            ValueError,
            match="Library conversion fallback snapshot is invalid",
        ):
            orchestrator.execute_attempt(
                session=session,
                workspace_id=workspace.id,
                attempt_id=scheduled.attempt.id,
                execution_generation=1,
            )

        assert scheduled.attempt.status == "queued"
        assert version.processing_status == "queued"
        assert fallback.probe_calls == 0
        assert session.scalar(select(func.count(LibraryAttempt.id))) == 1


@pytest.mark.parametrize(
    ("field", "tampered_value"),
    [
        ("provider_id", "test.unexpected"),
        ("provider_version", "9.9.9"),
        ("provider_spi_version", "9.9"),
        ("config_hash", "b" * 64),
        ("input_sha256", "f" * 64),
        ("output_schema_version", "9.9"),
    ],
)
def test_tampered_attempt_recipe_is_rejected_before_converter_execution(
    session_factory,
    tmp_path: Path,
    limits: ConversionLimits,
    field: str,
    tampered_value: str,
) -> None:
    plugin = _ScriptedConverter(
        "test.recipe",
        priority=100,
        outcomes=(ConversionResult(markdown="must not run"),),
    )
    orchestrator, store = _orchestrator(tmp_path, plugin)

    with session_factory.begin() as session:
        workspace, item, version = _persist_source(session, store)
        scheduled = _schedule(orchestrator, session, workspace, version, limits)
        assert scheduled.attempt is not None
        setattr(scheduled.attempt, field, tampered_value)
        session.flush()

        result = orchestrator.execute_attempt(
            session=session,
            workspace_id=workspace.id,
            attempt_id=scheduled.attempt.id,
            execution_generation=1,
        )

        assert result.status == "failed"
        assert result.selected is False
        assert scheduled.attempt.status == "failed"
        assert scheduled.attempt.error_code == "converter_recipe_invalid"
        assert plugin.probe_calls == 0
        assert plugin.convert_calls == 0
        assert version.selected_conversion_attempt_id is None
        assert item.current_ready_version_id is None
        assert session.scalar(select(func.count(LibraryArtifact.id))) == 0


@pytest.mark.parametrize("preexisting", [False, True])
def test_stale_fence_before_materialization_never_publishes_output(
    session_factory,
    tmp_path: Path,
    limits: ConversionLimits,
    monkeypatch: pytest.MonkeyPatch,
    preexisting: bool,
) -> None:
    markdown = "# Stale materialization"
    output_bytes = markdown.encode("utf-8")
    output_sha256 = hashlib.sha256(output_bytes).hexdigest()
    plugin = _ScriptedConverter(
        "test.stale-blob",
        priority=100,
        outcomes=(ConversionResult(markdown=markdown),),
    )
    orchestrator, store = _orchestrator(tmp_path, plugin)

    with session_factory.begin() as session:
        workspace, _, version = _persist_source(session, store)
        scheduled = _schedule(orchestrator, session, workspace, version, limits)
        assert scheduled.attempt is not None
        output_path = store.path_for_key(
            store.storage_key_for_hash(workspace.id, output_sha256)
        )
        if preexisting:
            existing = store.put_bytes(
                output_bytes,
                workspace_id=workspace.id,
                max_bytes=limits.max_output_bytes,
            )
            assert existing.created is True
            assert output_path.is_file()
        else:
            assert not output_path.exists()

        original_convert = plugin.convert
        original_put_bytes = store.put_bytes
        put_calls = 0

        def _convert_then_advance_fence(request: ConversionRequest):
            converted = original_convert(request)
            scheduled.attempt.execution_generation = 2
            return converted

        def _count_output_publications(
            content: bytes,
            *,
            workspace_id,
            max_bytes: int,
        ):
            nonlocal put_calls
            put_calls += 1
            return original_put_bytes(
                content,
                workspace_id=workspace_id,
                max_bytes=max_bytes,
            )

        monkeypatch.setattr(plugin, "convert", _convert_then_advance_fence)
        monkeypatch.setattr(store, "put_bytes", _count_output_publications)

        result = orchestrator.execute_attempt(
            session=session,
            workspace_id=workspace.id,
            attempt_id=scheduled.attempt.id,
            execution_generation=1,
        )

        assert result.status == "stale"
        assert result.selected is False
        assert plugin.convert_calls == 1
        assert put_calls == 0
        assert scheduled.attempt.execution_generation == 2
        assert session.scalar(select(func.count(LibraryArtifact.id))) == 0
        assert (
            session.scalar(
                select(func.count(StoredBlobRow.id)).where(
                    StoredBlobRow.workspace_id == workspace.id,
                    StoredBlobRow.sha256 == output_sha256,
                )
            )
            == 0
        )
        assert output_path.exists() is preexisting


@pytest.mark.parametrize("preexisting", [False, True])
def test_partial_staging_failure_never_publishes_new_output_blob(
    session_factory,
    tmp_path: Path,
    limits: ConversionLimits,
    monkeypatch: pytest.MonkeyPatch,
    preexisting: bool,
) -> None:
    markdown = "# Materialized before failure"
    markdown_bytes = markdown.encode("utf-8")
    markdown_sha256 = hashlib.sha256(markdown_bytes).hexdigest()
    rejected_asset = b"fail this asset write"
    plugin = _ScriptedConverter(
        "test.partial-materialization",
        priority=100,
        outcomes=(
            ConversionResult(
                markdown=markdown,
                assets=(
                    ProducedAsset(
                        role="asset",
                        media_type="text/plain",
                        content=rejected_asset,
                        filename="asset.txt",
                    ),
                ),
            ),
        ),
    )
    orchestrator, store = _orchestrator(tmp_path, plugin)

    with session_factory.begin() as session:
        workspace, _, version = _persist_source(session, store)
        scheduled = _schedule(orchestrator, session, workspace, version, limits)
        assert scheduled.attempt is not None
        markdown_path = store.path_for_key(
            store.storage_key_for_hash(workspace.id, markdown_sha256)
        )
        if preexisting:
            existing = store.put_bytes(
                markdown_bytes,
                workspace_id=workspace.id,
                max_bytes=limits.max_output_bytes,
            )
            assert existing.created is True
            assert markdown_path.is_file()
        else:
            assert not markdown_path.exists()

        original_stage_chunks = store.stage_chunks

        def _fail_asset_staging(
            chunks,
            *,
            workspace_id,
            max_bytes: int,
        ):
            buffered = tuple(chunks)
            content = b"".join(buffered)
            if content == rejected_asset:
                raise RuntimeError("injected asset staging failure")
            return original_stage_chunks(
                buffered,
                workspace_id=workspace_id,
                max_bytes=max_bytes,
            )

        monkeypatch.setattr(store, "stage_chunks", _fail_asset_staging)

        result = orchestrator.execute_attempt(
            session=session,
            workspace_id=workspace.id,
            attempt_id=scheduled.attempt.id,
            execution_generation=1,
        )

        assert result.status == "requeued"
        assert scheduled.attempt.status == "queued"
        assert scheduled.attempt.error_code == "converter_materialization_failed"
        assert scheduled.attempt.execution_generation == 2
        assert session.scalar(select(func.count(LibraryArtifact.id))) == 0
        assert (
            session.scalar(
                select(func.count(StoredBlobRow.id)).where(
                    StoredBlobRow.workspace_id == workspace.id,
                    StoredBlobRow.sha256 == markdown_sha256,
                )
            )
            == 0
        )
        assert markdown_path.exists() is preexisting
        assert not list(store.staging_root.rglob("*.stage"))


def test_late_older_run_cannot_replace_newer_same_version_selection(
    session_factory,
    tmp_path: Path,
    limits: ConversionLimits,
) -> None:
    plugin = _ScriptedConverter(
        "test.race",
        priority=100,
        outcomes=(
            ConversionResult(markdown="# Newer run"),
            ConversionResult(markdown="# Older run completed late"),
        ),
    )
    orchestrator, store = _orchestrator(tmp_path, plugin)

    with session_factory.begin() as session:
        workspace, _, version = _persist_source(session, store)
        initial = _schedule(orchestrator, session, workspace, version, limits)
        assert initial.attempt is not None
        _, newer_attempt = _add_manual_attempt(
            session,
            version=version,
            template_run=initial.run,
            run_seq=2,
        )

        newer = orchestrator.execute_attempt(
            session=session,
            workspace_id=workspace.id,
            attempt_id=newer_attempt.id,
            execution_generation=1,
        )
        older = orchestrator.execute_attempt(
            session=session,
            workspace_id=workspace.id,
            attempt_id=initial.attempt.id,
            execution_generation=1,
        )

        assert newer.selected is True
        assert older.status == "ready"
        assert older.selected is False
        assert version.selected_conversion_attempt_id == newer_attempt.id
        assert (
            session.scalar(
                select(func.count(LibraryProjectionOutbox.id)).where(
                    LibraryProjectionOutbox.conversion_attempt_id == newer_attempt.id
                )
            )
            == 1
        )
        assert (
            session.scalar(
                select(func.count(LibraryProjectionOutbox.id)).where(
                    LibraryProjectionOutbox.conversion_attempt_id == initial.attempt.id
                )
            )
            == 0
        )


def test_late_older_retryable_failure_cannot_downgrade_newer_ready_run(
    session_factory,
    tmp_path: Path,
    limits: ConversionLimits,
) -> None:
    plugin = _ScriptedConverter(
        "test.late-failure",
        priority=100,
        outcomes=(
            ConversionResult(markdown="# Newer run wins"),
            ConversionError(
                "transport_failure",
                "Late older failure.",
                retryable=True,
                fallback_allowed=True,
            ),
        ),
    )
    orchestrator, store = _orchestrator(tmp_path, plugin)

    with session_factory.begin() as session:
        workspace, _, version = _persist_source(session, store)
        initial = _schedule(orchestrator, session, workspace, version, limits)
        assert initial.attempt is not None
        _, newer_attempt = _add_manual_attempt(
            session,
            version=version,
            template_run=initial.run,
            run_seq=2,
        )

        newer = orchestrator.execute_attempt(
            session=session,
            workspace_id=workspace.id,
            attempt_id=newer_attempt.id,
            execution_generation=1,
        )
        older = orchestrator.execute_attempt(
            session=session,
            workspace_id=workspace.id,
            attempt_id=initial.attempt.id,
            execution_generation=1,
        )

        assert newer.status == "ready"
        assert newer.selected is True
        assert older.status == "stale"
        assert older.selected is False
        assert initial.attempt.status == "failed"
        assert initial.attempt.transport_retry_count == 0
        assert initial.run.status == "failed"
        assert version.processing_status == "ready"
        assert version.selected_conversion_attempt_id == newer_attempt.id


def test_late_terminal_marker_cannot_downgrade_newer_ready_run(
    session_factory,
    tmp_path: Path,
    limits: ConversionLimits,
) -> None:
    plugin = _ScriptedConverter(
        "test.late-terminal",
        priority=100,
        outcomes=(ConversionResult(markdown="# Newer run wins"),),
    )
    orchestrator, store = _orchestrator(tmp_path, plugin)

    with session_factory.begin() as session:
        workspace, _, version = _persist_source(session, store)
        initial = _schedule(orchestrator, session, workspace, version, limits)
        assert initial.attempt is not None
        _, newer_attempt = _add_manual_attempt(
            session,
            version=version,
            template_run=initial.run,
            run_seq=2,
        )
        newer = orchestrator.execute_attempt(
            session=session,
            workspace_id=workspace.id,
            attempt_id=newer_attempt.id,
            execution_generation=1,
        )
        assert newer.selected is True

        orchestrator.mark_terminal_failure(
            session=session,
            workspace_id=workspace.id,
            attempt_id=initial.attempt.id,
            execution_generation=1,
            manual_retryable=True,
        )

        assert initial.attempt.status == "failed"
        assert initial.run.status == "failed"
        assert version.processing_status == "ready"
        assert version.selected_conversion_attempt_id == newer_attempt.id


def test_same_version_reconversion_clears_previous_semantic_generation(
    session_factory,
    tmp_path: Path,
    limits: ConversionLimits,
) -> None:
    plugin = _ScriptedConverter(
        "test.same-version-semantic-fence",
        priority=100,
        outcomes=(
            ConversionResult(markdown="# Attempt A"),
            ConversionResult(markdown="# Attempt B"),
        ),
    )
    orchestrator, store = _orchestrator(tmp_path, plugin)

    with session_factory.begin() as session:
        workspace, item, version = _persist_source(session, store)
        initial = _schedule(orchestrator, session, workspace, version, limits)
        assert initial.attempt is not None
        assert (
            orchestrator.execute_attempt(
                session=session,
                workspace_id=workspace.id,
                attempt_id=initial.attempt.id,
                execution_generation=1,
            ).selected
            is True
        )
        session.add(
            ContentChunk(
                workspace_id=workspace.id,
                document_kind="library_item",
                document_id=item.id,
                content_scope="published",
                source_version_id=version.id,
                ordinal=0,
                heading_path="# Attempt A",
                char_start=0,
                char_end=9,
                chunk_text="Attempt A",
                chunk_text_hash="old-attempt-a",
                token_count=2,
                source_content_hash=version.content_hash,
            )
        )
        session.flush()

        _, replacement_attempt = _add_manual_attempt(
            session,
            version=version,
            template_run=initial.run,
            run_seq=2,
        )
        replacement = orchestrator.execute_attempt(
            session=session,
            workspace_id=workspace.id,
            attempt_id=replacement_attempt.id,
            execution_generation=1,
        )

        assert replacement.selected is True
        assert version.selected_conversion_attempt_id == replacement_attempt.id
        assert (
            session.scalar(
                select(func.count(ContentChunk.id)).where(
                    ContentChunk.document_kind == "library_item",
                    ContentChunk.document_id == item.id,
                )
            )
            == 0
        )
        assert (
            session.scalar(
                select(func.count(Job.id)).where(
                    Job.job_type == "embed",
                    Job.idempotency_key
                    == f"library-conversion:{replacement_attempt.id}",
                )
            )
            == 1
        )


def test_historical_version_reconversion_does_not_publish_current_projection(
    session_factory,
    tmp_path: Path,
    limits: ConversionLimits,
) -> None:
    plugin = _ScriptedConverter(
        "test.historical-reconversion",
        priority=100,
        outcomes=(
            ConversionResult(markdown="# Version one initial"),
            ConversionResult(markdown="# Version two current"),
            ConversionResult(markdown="# Version one historical reconversion"),
        ),
    )
    orchestrator, store = _orchestrator(tmp_path, plugin)

    with session_factory.begin() as session:
        workspace, item, version_one = _persist_source(session, store)
        first = _schedule(orchestrator, session, workspace, version_one, limits)
        assert first.attempt is not None
        assert (
            orchestrator.execute_attempt(
                session=session,
                workspace_id=workspace.id,
                attempt_id=first.attempt.id,
                execution_generation=1,
            ).selected
            is True
        )

        version_two = LibraryItemVersion(
            workspace_id=item.workspace_id,
            library_item_id=item.id,
            version_no=2,
            content_hash=version_one.content_hash,
            raw_storage_ref=None,
            source_blob_workspace_id=version_one.source_blob_workspace_id,
            source_blob_id=version_one.source_blob_id,
            source_blob_epoch=version_one.source_blob_epoch,
            original_filename="fixture-v2.testfmt",
            declared_media_type=_MEDIA_TYPE,
            detected_media_type=_MEDIA_TYPE,
            format_id=_FORMAT_ID,
            byte_size=version_one.byte_size,
            detection_json={"detector_id": "test", "confidence": 1.0},
            processing_status="pending",
        )
        session.add(version_two)
        session.flush()
        item.current_version_id = version_two.id
        second = _schedule(orchestrator, session, workspace, version_two, limits)
        assert second.attempt is not None
        assert (
            orchestrator.execute_attempt(
                session=session,
                workspace_id=workspace.id,
                attempt_id=second.attempt.id,
                execution_generation=1,
            ).selected
            is True
        )
        assert item.current_ready_version_id == version_two.id

        _, historical_attempt = _add_manual_attempt(
            session,
            version=version_one,
            template_run=first.run,
            run_seq=2,
        )
        historical = orchestrator.execute_attempt(
            session=session,
            workspace_id=workspace.id,
            attempt_id=historical_attempt.id,
            execution_generation=1,
        )

        assert historical.selected is True
        assert version_one.selected_conversion_attempt_id == historical_attempt.id
        assert item.current_ready_version_id == version_two.id
        assert (
            session.scalar(
                select(func.count(LibraryProjectionOutbox.id)).where(
                    LibraryProjectionOutbox.conversion_attempt_id
                    == historical_attempt.id
                )
            )
            == 0
        )
        assert (
            session.scalar(
                select(func.count(Job.id)).where(
                    Job.job_type == "embed",
                    Job.idempotency_key
                    == f"library-conversion:{historical_attempt.id}",
                )
            )
            == 0
        )


def test_two_phase_worker_commits_converting_state_before_converter_runs(
    tmp_path: Path,
    limits: ConversionLimits,
) -> None:
    entered = Event()
    release = Event()

    class _BlockingConverter(_ScriptedConverter):
        def convert(self, request: ConversionRequest) -> ConversionResult:
            entered.set()
            if not release.wait(timeout=5):
                raise RuntimeError("test did not release blocking converter")
            return super().convert(request)

    plugin = _BlockingConverter(
        "test.observable-running",
        priority=100,
        outcomes=(ConversionResult(markdown="# Observable completion"),),
    )
    orchestrator, store = _orchestrator(tmp_path, plugin)
    database_path = (tmp_path / "conversion-running.sqlite").as_posix()
    engine = create_engine(
        f"sqlite+pysqlite:///{database_path}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    worker_sessions = sessionmaker(bind=engine, expire_on_commit=False)
    orchestrator.durable_media_installer_factory = lambda _session, _workspace_id: (
        _durable_media_installer(store, worker_sessions)
    )
    with worker_sessions.begin() as session:
        seed_default_workspace(session)
        workspace, _, version = _persist_source(session, store)
        scheduled = _schedule(orchestrator, session, workspace, version, limits)
        assert scheduled.attempt is not None
        attempt_id = scheduled.attempt.id
        run_id = scheduled.run.id
        version_id = version.id
        workspace_id = workspace.id

    worker = OneShotWorker(job_queue=orchestrator.job_queue)
    worker.library_conversion_service = orchestrator
    results = []
    failures: list[BaseException] = []

    def _dispatch() -> None:
        try:
            results.append(
                worker.dispatch_once_with_session_factory(
                    worker_sessions,
                    workspace_id=workspace_id,
                    job_types={LIBRARY_CONVERSION_JOB_TYPE},
                )
            )
        except BaseException as exc:  # pragma: no cover - asserted below
            failures.append(exc)

    thread = Thread(target=_dispatch, name="conversion-observability")
    thread.start()
    try:
        assert entered.wait(timeout=5)
        with worker_sessions.begin() as session:
            attempt = session.get(LibraryAttempt, attempt_id)
            run = session.get(LibraryRun, run_id)
            version = session.get(LibraryItemVersion, version_id)
            assert attempt is not None and attempt.status == "running"
            assert attempt.started_at is not None
            assert run is not None and run.status == "running"
            assert version is not None and version.processing_status == "converting"
    finally:
        release.set()
        thread.join(timeout=10)
        engine.dispose()

    assert not thread.is_alive()
    assert failures == []
    assert len(results) == 1
    assert results[0] is not None and results[0].status == "completed"


def test_manual_retry_of_terminal_running_job_fences_late_phase_two_completion(
    tmp_path: Path,
    limits: ConversionLimits,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    phase_two_entered = Event()
    release_phase_two = Event()
    plugin = _ScriptedConverter(
        "test.manual-retry-before-late-phase-two",
        priority=100,
        outcomes=(
            ConversionError(
                "converter_process_failed",
                "Converter process failed.",
                retryable=True,
                fallback_allowed=False,
            ),
        ),
    )
    orchestrator, store = _orchestrator(tmp_path, plugin)
    database_path = (tmp_path / "manual-retry-late-phase-two.sqlite").as_posix()
    engine = create_engine(
        f"sqlite+pysqlite:///{database_path}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    worker_sessions = sessionmaker(bind=engine, expire_on_commit=False)
    with worker_sessions.begin() as session:
        seed_default_workspace(session)
        workspace, item, version = _persist_source(session, store)
        scheduled = _schedule(orchestrator, session, workspace, version, limits)
        assert scheduled.attempt is not None
        assert scheduled.job is not None
        transport_budget = scheduled.run.conversion_detail.per_provider_budget_json[
            "max_transport_retries"
        ]
        assert isinstance(transport_budget, int)
        scheduled.attempt.transport_retry_count = transport_budget
        workspace_id = workspace.id
        item_id = item.id
        version_id = version.id
        attempt_id = scheduled.attempt.id
        job_id = scheduled.job.id

    real_acquire_write_intent = worker_runtime.acquire_write_intent

    def hold_phase_two_before_job_lock(session: Session) -> None:
        phase_two_entered.set()
        if not release_phase_two.wait(timeout=5):
            raise RuntimeError("test did not release phase-two completion")
        real_acquire_write_intent(session)

    monkeypatch.setattr(
        worker_runtime,
        "acquire_write_intent",
        hold_phase_two_before_job_lock,
    )
    worker = OneShotWorker(job_queue=orchestrator.job_queue)
    worker.library_conversion_service = orchestrator
    results = []
    failures: list[BaseException] = []

    def dispatch_generation_one() -> None:
        try:
            results.append(
                worker.dispatch_once_with_session_factory(
                    worker_sessions,
                    workspace_id=workspace_id,
                    job_types={LIBRARY_CONVERSION_JOB_TYPE},
                )
            )
        except BaseException as exc:  # pragma: no cover - asserted below
            failures.append(exc)

    thread = Thread(target=dispatch_generation_one, name="late-phase-two")
    thread.start()
    try:
        assert phase_two_entered.wait(timeout=5)
        with worker_sessions.begin() as session:
            terminal_attempt = session.get(LibraryAttempt, attempt_id)
            running_job = session.get(Job, job_id)
            assert terminal_attempt is not None
            assert terminal_attempt.status == "failed"
            assert terminal_attempt.error_code == "converter_process_failed"
            assert running_job is not None and running_job.status == "running"

            retried = orchestrator.retry_failed_conversion(
                session=session,
                workspace_id=workspace_id,
                library_item_id=item_id,
                version_id=version_id,
                actor_kind="human",
                actor_id=str(uuid4()),
            )
            assert retried.attempt.execution_generation == 2
            assert retried.job.status == "queued"
            assert retried.job.target_ref["execution_generation"] == 2
    finally:
        release_phase_two.set()
        thread.join(timeout=10)

    assert not thread.is_alive()
    assert failures == []
    assert len(results) == 1
    assert results[0] is not None and results[0].status == "stale"
    with worker_sessions.begin() as session:
        attempt = session.get(LibraryAttempt, attempt_id)
        job = session.get(Job, job_id)
        assert attempt is not None and attempt.status == "queued"
        assert attempt.execution_generation == 2
        assert job is not None and job.status == "queued"
        assert job.target_ref["execution_generation"] == 2
        assert job.progress == 0
        assert job.completed_at is None
    engine.dispose()


def test_reclaimed_generation_is_not_completed_by_late_worker(
    tmp_path: Path,
    limits: ConversionLimits,
) -> None:
    entered = Event()
    release = Event()

    class _BlockingConverter(_ScriptedConverter):
        def convert(self, request: ConversionRequest) -> ConversionResult:
            entered.set()
            if not release.wait(timeout=5):
                raise RuntimeError("test did not release blocking converter")
            return super().convert(request)

    plugin = _BlockingConverter(
        "test.late-queue-completion",
        priority=100,
        outcomes=(ConversionResult(markdown="# Late generation one"),),
    )
    orchestrator, store = _orchestrator(tmp_path, plugin)
    database_path = (tmp_path / "late-queue-completion.sqlite").as_posix()
    engine = create_engine(
        f"sqlite+pysqlite:///{database_path}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    worker_sessions = sessionmaker(bind=engine, expire_on_commit=False)
    orchestrator.durable_media_installer_factory = lambda _session, _workspace_id: (
        _durable_media_installer(store, worker_sessions)
    )
    with worker_sessions.begin() as session:
        seed_default_workspace(session)
        workspace, _, version = _persist_source(session, store)
        scheduled = _schedule(orchestrator, session, workspace, version, limits)
        assert scheduled.attempt is not None
        assert scheduled.job is not None
        attempt_id = scheduled.attempt.id
        job_id = scheduled.job.id
        workspace_id = workspace.id

    worker = OneShotWorker(job_queue=orchestrator.job_queue)
    worker.library_conversion_service = orchestrator
    results = []
    failures: list[BaseException] = []

    def _dispatch_generation_one() -> None:
        try:
            results.append(
                worker.dispatch_once_with_session_factory(
                    worker_sessions,
                    workspace_id=workspace_id,
                    job_types={LIBRARY_CONVERSION_JOB_TYPE},
                )
            )
        except BaseException as exc:  # pragma: no cover - asserted below
            failures.append(exc)

    thread = Thread(target=_dispatch_generation_one, name="late-generation-one")
    thread.start()
    try:
        assert entered.wait(timeout=5)
        with worker_sessions.begin() as session:
            running_job = session.get(Job, job_id)
            assert running_job is not None
            running_job.started_at = utc_now() - timedelta(seconds=30)

        assert (
            worker.reclaim_stale_jobs_with_session_factory(
                worker_sessions,
                lease_timeout_seconds=1,
            )
            == 1
        )
        with worker_sessions.begin() as session:
            generation_two = orchestrator.job_queue.claim_next(
                session=session,
                workspace_id=workspace_id,
                job_types={LIBRARY_CONVERSION_JOB_TYPE},
            )
            assert generation_two is not None
            assert generation_two.id == job_id
            assert generation_two.target_ref["execution_generation"] == 2
    finally:
        release.set()
        thread.join(timeout=10)

    assert not thread.is_alive()
    assert failures == []
    assert len(results) == 1
    assert results[0] is not None and results[0].status == "stale"
    assert results[0].detail["conversion_status"] == "stale"
    with worker_sessions.begin() as session:
        attempt = session.get(LibraryAttempt, attempt_id)
        job = session.get(Job, job_id)
        assert attempt is not None
        assert attempt.execution_generation == 2
        assert attempt.status == "queued"
        assert job is not None
        assert job.status == "running"
        assert job.retry_count == 1
        assert job.target_ref["execution_generation"] == 2
    engine.dispose()


def test_reclaimed_generation_is_not_failed_by_late_worker(
    session_factory,
    tmp_path: Path,
    limits: ConversionLimits,
) -> None:
    plugin = _ScriptedConverter(
        "test.late-queue-failure",
        priority=100,
        outcomes=(ConversionResult(markdown="# Not executed"),),
    )
    orchestrator, store = _orchestrator(tmp_path, plugin)
    worker = OneShotWorker(job_queue=orchestrator.job_queue)
    worker.library_conversion_service = orchestrator

    with session_factory.begin() as session:
        workspace, _, version = _persist_source(session, store)
        scheduled = _schedule(orchestrator, session, workspace, version, limits)
        assert scheduled.attempt is not None
        claimed = orchestrator.job_queue.claim_next(
            session=session,
            workspace_id=workspace.id,
            job_types={LIBRARY_CONVERSION_JOB_TYPE},
        )
        assert claimed is not None
        generation_one = _ClaimedJob.from_model(claimed)
        claimed.started_at = utc_now() - timedelta(seconds=30)
        assert orchestrator.mark_attempt_running(
            session=session,
            workspace_id=workspace.id,
            attempt_id=scheduled.attempt.id,
            execution_generation=1,
        )
        attempt_id = scheduled.attempt.id
        job_id = claimed.id
        workspace_id = workspace.id

    assert (
        worker.reclaim_stale_jobs_with_session_factory(
            session_factory,
            lease_timeout_seconds=1,
        )
        == 1
    )
    with session_factory.begin() as session:
        generation_two = orchestrator.job_queue.claim_next(
            session=session,
            workspace_id=workspace_id,
            job_types={LIBRARY_CONVERSION_JOB_TYPE},
        )
        assert generation_two is not None
        assert generation_two.id == job_id
        assert generation_two.target_ref["execution_generation"] == 2

    late_status = worker._mark_failed_isolated(
        session_factory=session_factory,
        workspace_id=workspace_id,
        job_id=job_id,
        error="late generation one failure",
        max_retries=3,
        expected_job=generation_one,
    )

    assert late_status == "stale"
    with session_factory.begin() as session:
        attempt = session.get(LibraryAttempt, attempt_id)
        job = session.get(Job, job_id)
        assert attempt is not None
        assert attempt.execution_generation == 2
        assert attempt.status == "queued"
        assert job is not None
        assert job.status == "running"
        assert job.retry_count == 1
        assert job.target_ref["execution_generation"] == 2
        assert job.last_error != "late generation one failure"

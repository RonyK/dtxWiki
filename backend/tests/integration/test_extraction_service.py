import hashlib
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from backend.models.audit import AuditEvent
from backend.models.base import Base
from backend.models.job import Job
from backend.models.library import (
    LibraryArtifact,
    LibraryArtifactConversionDetail,
    LibraryAttempt,
    LibraryAttemptConversionDetail,
    LibraryItem,
    LibraryItemVersion,
    LibraryRun,
    LibraryRunConversionDetail,
)
from backend.scripts.seed_dev import seed_default_workspace
from backend.services.extraction_service import ExtractionError, ExtractionService
from backend.services.job_queue import JobQueueService
from backend.services.library_service import LibraryService


def make_session_factory():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def test_extraction_creates_text_artifact_and_completes_job(media_installer_factory):
    session_factory = make_session_factory()
    library_service = LibraryService()
    queue = JobQueueService()
    extraction_service = ExtractionService(
        job_queue=queue, media_installer_factory=media_installer_factory
    )

    with session_factory.begin() as session:
        workspace = seed_default_workspace(session)
        registration = library_service.register_library_item(
            session=session,
            workspace_id=workspace.id,
            canonical_uri="https://example.com/extract",
            title="Extract",
            format_hint="markdown",
            raw_text="# Extract\n\nBody",
        )
        claimed = queue.claim_next(session=session, workspace_id=workspace.id)
        artifact = extraction_service.extract_library_item_version(
            session=session,
            library_item_version_id=registration.version.id,
            job=claimed,
        )

    with session_factory.begin() as session:
        stored_artifact = session.get(LibraryArtifact, artifact.id)
        version = session.get(LibraryItemVersion, registration.version.id)
        source = session.get(LibraryItem, registration.library_item.id)
        job = session.get(Job, registration.extraction_job.id)
        audit = session.scalar(
            select(AuditEvent).where(AuditEvent.action == "library.extraction.complete")
        )
        # conversion_detail is a lazy relationship -- read it while the
        # session is still open, since the assertions below run after this
        # block (and its session) has closed.
        detail = stored_artifact.conversion_detail
        extraction_method = detail.extraction_method
        index_text = detail.index_text
        location_model = detail.location_model

    assert stored_artifact.stage == "conversion"
    assert stored_artifact.role == "markdown"
    assert stored_artifact.attempt_id is not None
    assert stored_artifact.blob_id is not None
    assert stored_artifact.content_sha256 == hashlib.sha256(
        b"# Extract\n\nBody"
    ).hexdigest()
    assert extraction_method == "text/markdown"
    assert index_text == "# Extract\n\nBody"
    assert location_model == {
        "kind": "plain_text",
        "line_count": 3,
        "character_count": 15,
    }
    assert version.processing_status == "completed"
    assert version.extraction_summary == "extracted 15 characters"
    assert source.lifecycle_status == "ready"
    assert source.current_ready_version_id == version.id
    assert job.status == "completed"
    assert job.progress == 100
    assert audit.target_id == version.id
    assert audit.payload["artifact_id"] == str(stored_artifact.id)


def test_extraction_is_idempotent_for_existing_artifact(media_installer_factory):
    session_factory = make_session_factory()
    library_service = LibraryService()
    queue = JobQueueService()
    extraction_service = ExtractionService(
        job_queue=queue, media_installer_factory=media_installer_factory
    )

    with session_factory.begin() as session:
        workspace = seed_default_workspace(session)
        registration = library_service.register_library_item(
            session=session,
            workspace_id=workspace.id,
            canonical_uri="https://example.com/idempotent",
            title="Idempotent",
            raw_text="same body",
        )
        first_job = queue.claim_next(session=session, workspace_id=workspace.id)
        first = extraction_service.extract_library_item_version(
            session=session,
            library_item_version_id=registration.version.id,
            job=first_job,
        )
        second = extraction_service.extract_library_item_version(
            session=session,
            library_item_version_id=registration.version.id,
        )

    with session_factory.begin() as session:
        artifacts = session.scalars(select(LibraryArtifact)).all()

    assert first.id == second.id
    assert len(artifacts) == 1


def test_mark_version_failed_persists_markers_and_audit(media_installer_factory):
    session_factory = make_session_factory()
    library_service = LibraryService()
    extraction_service = ExtractionService(
        media_installer_factory=media_installer_factory
    )

    with session_factory.begin() as session:
        workspace = seed_default_workspace(session)
        registration = library_service.register_library_item(
            session=session,
            workspace_id=workspace.id,
            canonical_uri="https://example.com/mark-failed",
            title="Mark Failed",
            raw_text="body",
        )
        extraction_service.mark_version_failed(
            session=session,
            library_item_version_id=registration.version.id,
            error="simulated rollback recovery",
        )

    with session_factory.begin() as session:
        version = session.get(LibraryItemVersion, registration.version.id)
        source = session.get(LibraryItem, registration.library_item.id)
        failed_audit = session.scalar(
            select(AuditEvent).where(AuditEvent.action == "library.extraction.failed")
        )

    assert version.processing_status == "failed"
    assert version.extraction_summary == "simulated rollback recovery"
    assert source.lifecycle_status == "extract_failed"
    assert failed_audit.target_id == version.id
    assert failed_audit.payload["error"] == "simulated rollback recovery"


def test_mark_version_failed_skips_completed_version(media_installer_factory):
    session_factory = make_session_factory()
    library_service = LibraryService()
    extraction_service = ExtractionService(
        media_installer_factory=media_installer_factory
    )

    with session_factory.begin() as session:
        workspace = seed_default_workspace(session)
        registration = library_service.register_library_item(
            session=session,
            workspace_id=workspace.id,
            canonical_uri="https://example.com/mark-failed-completed",
            title="Mark Failed Completed",
            raw_text="completed body",
        )
        extraction_service.extract_library_item_version(
            session=session,
            library_item_version_id=registration.version.id,
        )
        extraction_service.mark_version_failed(
            session=session,
            library_item_version_id=registration.version.id,
            error="stale failure",
        )

    with session_factory.begin() as session:
        version = session.get(LibraryItemVersion, registration.version.id)
        source = session.get(LibraryItem, registration.library_item.id)
        failed_audit = session.scalar(
            select(AuditEvent).where(AuditEvent.action == "library.extraction.failed")
        )

    assert version.processing_status == "completed"
    assert source.lifecycle_status == "ready"
    assert failed_audit is None


def test_mark_version_failed_is_noop_for_missing_version(media_installer_factory):
    session_factory = make_session_factory()
    extraction_service = ExtractionService(
        media_installer_factory=media_installer_factory
    )

    with session_factory.begin() as session:
        seed_default_workspace(session)
        extraction_service.mark_version_failed(
            session=session,
            library_item_version_id=uuid4(),
            error="no such version",
        )

    with session_factory.begin() as session:
        failed_audit = session.scalar(
            select(AuditEvent).where(AuditEvent.action == "library.extraction.failed")
        )

    assert failed_audit is None


def test_extraction_failure_marks_version_source_and_job_failed(
    media_installer_factory,
):
    session_factory = make_session_factory()
    library_service = LibraryService()
    queue = JobQueueService()
    extraction_service = ExtractionService(
        job_queue=queue, media_installer_factory=media_installer_factory
    )

    with session_factory.begin() as session:
        workspace = seed_default_workspace(session)
        registration = library_service.register_library_item(
            session=session,
            workspace_id=workspace.id,
            canonical_uri="https://example.com/missing",
            title="Missing",
            raw_storage_ref="missing://raw",
        )
        claimed = queue.claim_next(session=session, workspace_id=workspace.id)
        with pytest.raises(ExtractionError, match="unsupported source storage ref"):
            extraction_service.extract_library_item_version(
                session=session,
                library_item_version_id=registration.version.id,
                job=claimed,
            )

    with session_factory.begin() as session:
        version = session.get(LibraryItemVersion, registration.version.id)
        source = session.get(LibraryItem, registration.library_item.id)
        job = session.get(Job, registration.extraction_job.id)
        failed_audit = session.scalar(
            select(AuditEvent).where(AuditEvent.action == "library.extraction.failed")
        )

    assert version.processing_status == "failed"
    assert version.extraction_summary == "unsupported source storage ref: missing://raw"
    assert source.lifecycle_status == "extract_failed"
    assert job.status == "failed"
    assert job.retry_count == 1
    assert job.last_error == "unsupported source storage ref: missing://raw"
    assert failed_audit.target_id == version.id


def test_failed_newer_version_preserves_previous_ready_version(media_installer_factory):
    session_factory = make_session_factory()
    library_service = LibraryService()
    extraction_service = ExtractionService(
        media_installer_factory=media_installer_factory
    )

    with session_factory.begin() as session:
        workspace = seed_default_workspace(session)
        first = library_service.register_library_item(
            session=session,
            workspace_id=workspace.id,
            canonical_uri="https://example.com/last-ready",
            title="Last Ready",
            raw_text="first ready body",
        )
        extraction_service.extract_library_item_version(
            session=session,
            library_item_version_id=first.version.id,
        )
        second = library_service.register_library_item(
            session=session,
            workspace_id=workspace.id,
            canonical_uri="https://example.com/last-ready",
            title="Last Ready",
            raw_text="new pending body",
        )
        assert second.version.version_no > first.version.version_no
        extraction_service.mark_version_failed(
            session=session,
            library_item_version_id=second.version.id,
            error="new source failed",
        )

    with session_factory.begin() as session:
        source = session.get(LibraryItem, first.library_item.id)
        first_version = session.get(LibraryItemVersion, first.version.id)
        second_version = session.get(LibraryItemVersion, second.version.id)

    assert source.current_ready_version_id == first_version.id
    assert source.lifecycle_status == "ready"
    assert first_version.processing_status == "completed"
    assert second_version.processing_status == "failed"


def test_late_older_success_cannot_replace_newer_ready_with_stale_identity(
    tmp_path, media_installer_factory
):
    database_path = tmp_path / "legacy-extraction-cas.sqlite"
    engine = create_engine(f"sqlite+pysqlite:///{database_path.as_posix()}")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    library_service = LibraryService()
    extraction_service = ExtractionService(
        media_installer_factory=media_installer_factory
    )

    with session_factory.begin() as session:
        workspace = seed_default_workspace(session)
        first = library_service.register_library_item(
            session=session,
            workspace_id=workspace.id,
            canonical_uri="https://example.com/ready-cas",
            title="Ready CAS",
            raw_text="older body",
        )
        second = library_service.register_library_item(
            session=session,
            workspace_id=workspace.id,
            canonical_uri="https://example.com/ready-cas",
            title="Ready CAS",
            raw_text="newer body",
        )
        item_id = first.library_item.id
        first_version_id = first.version.id
        second_version_id = second.version.id

    stale_session = session_factory()
    try:
        stale_item = stale_session.get(LibraryItem, item_id)
        assert stale_item.current_ready_version_id is None
        stale_session.commit()

        with session_factory.begin() as winner_session:
            extraction_service.extract_library_item_version(
                session=winner_session,
                library_item_version_id=second_version_id,
            )

        with stale_session.begin():
            extraction_service.extract_library_item_version(
                session=stale_session,
                library_item_version_id=first_version_id,
            )
    finally:
        stale_session.close()
        engine.dispose()

    verification_engine = create_engine(
        f"sqlite+pysqlite:///{database_path.as_posix()}"
    )
    verification_session_factory = sessionmaker(
        bind=verification_engine,
        expire_on_commit=False,
    )
    try:
        with verification_session_factory.begin() as session:
            source = session.get(LibraryItem, item_id)
            first_version = session.get(LibraryItemVersion, first_version_id)
            second_version = session.get(LibraryItemVersion, second_version_id)
        assert source.current_ready_version_id == second_version.id
        assert first_version.processing_status == "completed"
    finally:
        verification_engine.dispose()


@pytest.mark.parametrize("lifecycle_status", ["archived", "hidden"])
def test_success_does_not_resurrect_excluded_lifecycle(
    lifecycle_status, media_installer_factory
):
    session_factory = make_session_factory()
    library_service = LibraryService()
    extraction_service = ExtractionService(
        media_installer_factory=media_installer_factory
    )

    with session_factory.begin() as session:
        workspace = seed_default_workspace(session)
        registration = library_service.register_library_item(
            session=session,
            workspace_id=workspace.id,
            canonical_uri=f"https://example.com/{lifecycle_status}",
            title=lifecycle_status.title(),
            raw_text="body",
        )
        registration.library_item.lifecycle_status = lifecycle_status
        extraction_service.extract_library_item_version(
            session=session,
            library_item_version_id=registration.version.id,
        )

    with session_factory.begin() as session:
        source = session.get(LibraryItem, registration.library_item.id)
        version = session.get(LibraryItemVersion, registration.version.id)
        embed_job = session.scalar(select(Job).where(Job.job_type == "embed"))

    assert source.lifecycle_status == lifecycle_status
    assert source.current_ready_version_id == version.id
    assert version.processing_status == "completed"
    assert embed_job is None


def test_idempotent_legacy_retry_does_not_overwrite_selected_conversion_state(
    media_installer_factory,
):
    session_factory = make_session_factory()
    library_service = LibraryService()
    extraction_service = ExtractionService(
        media_installer_factory=media_installer_factory
    )

    with session_factory.begin() as session:
        workspace = seed_default_workspace(session)
        registration = library_service.register_library_item(
            session=session,
            workspace_id=workspace.id,
            canonical_uri="https://example.com/selected-conversion",
            title="Selected Conversion",
            raw_text="legacy body",
        )
        legacy_artifact = extraction_service.extract_library_item_version(
            session=session,
            library_item_version_id=registration.version.id,
        )
        run = LibraryRun(
            workspace_id=workspace.id,
            library_item_version_id=registration.version.id,
            stage="conversion",
            # extraction_service's own extract_library_item_version call
            # above already created run_seq=1 for this version.
            run_seq=2,
            reason="initial",
            status="completed",
            provider_chain_json=[],
            policy_revision="test-policy",
            output_schema_version="1",
            idempotency_key=f"selected:{registration.version.id}",
        )
        run.conversion_detail = LibraryRunConversionDetail(
            error_fallback_matrix_json={},
            per_provider_budget_json={},
            total_budget_json={},
        )
        session.add(run)
        session.flush()
        attempt = LibraryAttempt(
            workspace_id=workspace.id,
            run_id=run.id,
            library_item_version_id=registration.version.id,
            stage="conversion",
            provider_id="test.converter",
            provider_version="1.0.0",
            provider_spi_version="1",
            config_hash="test-config",
            input_sha256=registration.version.content_hash,
            output_schema_version="1",
            status="completed",
            chain_index=0,
            transport_retry_count=0,
            execution_generation=1,
            warnings_json=[],
            provenance_json={},
            metrics_json={},
        )
        attempt.conversion_detail = LibraryAttemptConversionDetail(
            manual_retry_disposition=None,
            disposition_reason=None,
        )
        session.add(attempt)
        session.flush()
        # This engine is created directly with create_engine(), bypassing
        # backend.app.db's PRAGMA foreign_keys=ON -- blob_workspace_id/
        # blob_id/blob_epoch's FK to blob_incarnation is unenforced here, so
        # a fabricated blob identity is fine for exercising the CAS logic
        # under test (no real Shared Media blob is needed).
        selected_artifact = LibraryArtifact(
            workspace_id=workspace.id,
            library_item_version_id=registration.version.id,
            attempt_id=attempt.id,
            stage="conversion",
            blob_workspace_id=workspace.id,
            blob_id=uuid4(),
            blob_epoch=1,
            role="markdown",
            kind="markdown",
            media_type="text/markdown",
            content_sha256="a" * 64,
            byte_size=15,
            ordinal=0,
        )
        selected_artifact.conversion_detail = LibraryArtifactConversionDetail(
            is_complete=True,
            sanitizer_version="test",
            extraction_method="text/markdown",
            index_text="# Converted",
            location_model={},
        )
        session.add(selected_artifact)
        session.flush()
        registration.version.selected_conversion_attempt_id = attempt.id
        registration.version.processing_status = "ready"
        registration.version.extraction_summary = "owned by conversion"
        session.flush()

        retried = extraction_service.extract_library_item_version(
            session=session,
            library_item_version_id=registration.version.id,
        )
        assert retried.id == selected_artifact.id

    with session_factory.begin() as session:
        source = session.get(LibraryItem, registration.library_item.id)
        version = session.get(LibraryItemVersion, registration.version.id)
        artifacts = session.scalars(
            select(LibraryArtifact).where(
                LibraryArtifact.library_item_version_id == version.id
            )
        ).all()

    assert source.current_ready_version_id == version.id
    assert version.selected_conversion_attempt_id == attempt.id
    assert version.processing_status == "ready"
    assert version.extraction_summary == "owned by conversion"
    assert {artifact.id for artifact in artifacts} == {
        legacy_artifact.id,
        selected_artifact.id,
    }

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError

from backend.app.main import create_app
from backend.app.settings import Settings
from backend.models.audit import AuditEvent
from backend.models.job import Job
from backend.models.library import (
    LibraryAttempt,
    LibraryItem,
    LibraryItemVersion,
    LibraryRun,
)
from backend.models.media import MediaInstallRequest, StoredBlobRow
from backend.models.user import User
from backend.models.workspace import Workspace
from backend.policy.rbac import grant_workspace_membership
from backend.services.conversion_orchestrator import LIBRARY_CONVERSION_JOB_TYPE
from backend.services.job_queue import JobQueueService
from backend.services.library_ingest import LibraryUploadAdmissionService
from backend.tests.conftest import (
    authenticate_test_client_in_workspace,
    captured_log_messages,
)

UPLOAD_OPERATION_HEADER = "X-DTX-Library-Upload-Operation-Id"


@dataclass(frozen=True, slots=True)
class UploadApi:
    client: TestClient
    workspace_id: UUID
    session_factory: object


@pytest.fixture
def upload_api(page_service, tmp_path: Path) -> Iterator[UploadApi]:
    settings = Settings(
        database_url="sqlite+pysqlite:///:memory:",
        session_secret="library-upload-p0-test-secret",
        library_blob_root=str(tmp_path / "library-blobs"),
        library_upload_hard_cap_bytes=10_000_000,
    )
    with TestClient(
        create_app(settings=settings, page_service=page_service),
        raise_server_exceptions=False,
    ) as client:
        workspace_id = UUID(
            authenticate_test_client_in_workspace(
                client,
                page_service.session_factory,
                email="library-upload-p0-owner@example.com",
                workspace_slug="default",
            )
        )
        yield UploadApi(client, workspace_id, page_service.session_factory)


def _upload(
    fixture: UploadApi,
    *,
    filename: str,
    content: bytes,
    operation_id: UUID | None = None,
    parent_node_id: UUID | None = None,
    request_id: str | None = None,
):
    headers: dict[str, str] = {}
    if operation_id is not None:
        headers[UPLOAD_OPERATION_HEADER] = str(operation_id)
    if request_id is not None:
        headers["X-Request-Id"] = request_id
    return fixture.client.post(
        f"/api/v1/workspaces/{fixture.workspace_id}/library/items",
        files={"file": (filename, content, "text/markdown")},
        data=(
            {"parent_node_id": str(parent_node_id)}
            if parent_node_id is not None
            else None
        ),
        headers=headers or None,
    )


def _add_version(
    fixture: UploadApi,
    *,
    item_id: UUID,
    filename: str,
    content: bytes,
    operation_id: UUID | None = None,
    request_id: str | None = None,
):
    """P4.2: POST library/items/{item_id}/versions -- the explicit
    add-a-version-to-this-exact-item command. Never a filename lookup."""

    headers: dict[str, str] = {}
    if operation_id is not None:
        headers[UPLOAD_OPERATION_HEADER] = str(operation_id)
    if request_id is not None:
        headers["X-Request-Id"] = request_id
    return fixture.client.post(
        f"/api/v1/workspaces/{fixture.workspace_id}/library/items/{item_id}/versions",
        files={"file": (filename, content, "text/markdown")},
        headers=headers or None,
    )


def test_empty_upload_has_stable_rejection_and_no_source_residue(
    upload_api: UploadApi,
) -> None:
    response = _upload(upload_api, filename="empty.txt", content=b"")

    assert response.status_code == 422, response.text
    assert response.json()["error_code"] == "library_file_empty"
    UUID(response.headers[UPLOAD_OPERATION_HEADER])
    with upload_api.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(LibraryItem)) == 0
        assert session.scalar(select(func.count()).select_from(StoredBlobRow)) == 0
        assert (
            session.scalar(select(func.count()).select_from(MediaInstallRequest)) == 0
        )


def test_same_digest_different_filename_and_actor_deduplicates_without_conflict(
    upload_api: UploadApi,
) -> None:
    content = "# Shared bytes\n\n한글 😀\n".encode()
    first = _upload(upload_api, filename="first-name.md", content=content)
    assert first.status_code == 201, first.text

    authenticate_test_client_in_workspace(
        upload_api.client,
        upload_api.session_factory,
        email="library-upload-p0-second-owner@example.com",
        workspace_slug="default",
    )
    second = _upload(upload_api, filename="renamed.md", content=content)

    assert second.status_code == 201, second.text
    assert second.json()["data"]["id"] != first.json()["data"]["id"]
    assert (
        second.json()["data"]["current_version"]["content_hash"]
        == first.json()["data"]["current_version"]["content_hash"]
    )
    assert (
        first.headers[UPLOAD_OPERATION_HEADER]
        != second.headers[UPLOAD_OPERATION_HEADER]
    )
    with upload_api.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(StoredBlobRow)) == 1
        assert session.scalar(select(func.count()).select_from(LibraryItem)) == 2
        assert session.scalar(select(func.count()).select_from(LibraryItemVersion)) == 2


def test_add_version_reusing_an_older_content_hash_moves_current_version_back(
    upload_api: UploadApi,
) -> None:
    original_content = b"# Version A\n"
    created = _upload(upload_api, filename="reuse.md", content=original_content)
    assert created.status_code == 201, created.text
    item_id = UUID(created.json()["data"]["id"])
    version_a_id = UUID(created.json()["data"]["current_version"]["id"])

    superseding = _add_version(
        upload_api,
        item_id=item_id,
        filename="reuse.md",
        content=b"# Version B\n",
    )
    assert superseding.status_code == 201, superseding.text
    version_b_id = UUID(superseding.json()["data"]["current_version"]["id"])
    assert version_b_id != version_a_id

    reused = _add_version(
        upload_api,
        item_id=item_id,
        filename="reuse.md",
        content=original_content,
    )

    # Reusing an existing version's content hash is a 200 (no new version
    # row), unlike the 201 fresh-content case above.
    assert reused.status_code == 200, reused.text
    assert UUID(reused.json()["data"]["current_version"]["id"]) == version_a_id
    with upload_api.session_factory() as session:
        item = session.get(LibraryItem, item_id)
        assert item is not None
        assert item.current_version_id == version_a_id


def test_upload_operation_exact_replay_converges_and_changed_facts_conflict(
    upload_api: UploadApi,
) -> None:
    operation_id = uuid4()
    first = _upload(
        upload_api,
        filename="operation.md",
        content=b"# First operation\n",
        operation_id=operation_id,
    )
    replay = _upload(
        upload_api,
        filename="operation.md",
        content=b"# First operation\n",
        operation_id=operation_id,
    )
    conflict = _upload(
        upload_api,
        filename="operation.md",
        content=b"# Changed operation\n",
        operation_id=operation_id,
    )
    # P4.2: placement is now validated unconditionally on every call to
    # library/items (not just "if no existing item was found by filename",
    # since there is no such lookup any more) -- an invalid parent_node_id
    # is rejected before idempotency-replay logic is ever reached, on a
    # replay or not. This is a fail-fast invariant, not an idempotency
    # conflict: the request never gets far enough to compare facts against
    # a prior install.
    invalid_placement = _upload(
        upload_api,
        filename="operation.md",
        content=b"# First operation\n",
        operation_id=operation_id,
        parent_node_id=uuid4(),
    )

    assert first.status_code == 201, first.text
    assert replay.status_code == 200, replay.text
    assert replay.json()["data"]["id"] == first.json()["data"]["id"]
    assert (
        replay.json()["data"]["current_version"]["id"]
        == first.json()["data"]["current_version"]["id"]
    )
    assert replay.headers[UPLOAD_OPERATION_HEADER] == str(operation_id)
    assert conflict.status_code == 409, conflict.text
    assert conflict.json()["error_code"] == "library_upload_idempotency_conflict"
    assert conflict.headers[UPLOAD_OPERATION_HEADER] == str(operation_id)
    assert invalid_placement.status_code == 404, invalid_placement.text
    assert invalid_placement.json()["error_code"] == "not_found"
    with upload_api.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(LibraryItemVersion)) == 1
        assert (
            session.scalar(select(func.count()).select_from(MediaInstallRequest)) == 1
        )


def test_expected_storage_and_sqlite_busy_failures_have_retry_contract(
    upload_api: UploadApi,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def storage_unavailable(*_args, **_kwargs):
        raise OSError(r"C:\private\library-source.bin")

    monkeypatch.setattr(LibraryUploadAdmissionService, "admit", storage_unavailable)
    with captured_log_messages("backend.api.read") as messages:
        storage = _upload(
            upload_api,
            filename="storage.md",
            content=b"# Storage\n",
            request_id="70000000-0000-0000-0000-000000000001",
        )
    assert storage.status_code == 503, storage.text
    assert storage.json()["error_code"] == "library_storage_temporarily_unavailable"
    assert storage.json()["request_id"] == "70000000-0000-0000-0000-000000000001"
    assert storage.headers["Retry-After"] == "1"
    UUID(storage.headers[UPLOAD_OPERATION_HEADER])
    storage_record = json.loads(messages[0])
    assert storage_record == {
        "request_id": "70000000-0000-0000-0000-000000000001",
        "stage": "library_source_admission",
        "error_code": "library_storage_temporarily_unavailable",
    }
    assert "C:\\private" not in messages[0]

    monkeypatch.undo()
    services = upload_api.client.app.state.container.get_services()

    def database_busy(*_args, **_kwargs):
        raise OperationalError(
            "BEGIN IMMEDIATE",
            {},
            sqlite3.OperationalError("database is locked"),
        )

    monkeypatch.setattr(
        services.library_binary_registration,
        "ensure_source_quota_capacity",
        database_busy,
    )
    database = _upload(
        upload_api,
        filename="database.md",
        content=b"# Database\n",
        request_id="70000000-0000-0000-0000-000000000002",
    )
    assert database.status_code == 503, database.text
    assert database.json()["error_code"] == "library_storage_temporarily_unavailable"
    assert database.json()["request_id"] == "70000000-0000-0000-0000-000000000002"
    assert database.headers["Retry-After"] == "1"
    UUID(database.headers[UPLOAD_OPERATION_HEADER])


def test_post_commit_projection_failure_replays_to_the_existing_source(
    upload_api: UploadApi,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    operation_id = uuid4()
    services = upload_api.client.app.state.container.get_services()
    original_get = services.domain_read.get_library_item
    calls = 0

    def fail_projection_once(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError(r"C:\private\projection.db")
        return original_get(*args, **kwargs)

    monkeypatch.setattr(services.domain_read, "get_library_item", fail_projection_once)
    first = _upload(
        upload_api,
        filename="projection-replay.md",
        content=b"# Projection replay\n",
        operation_id=operation_id,
        request_id="70000000-0000-0000-0000-000000000003",
    )
    replay = _upload(
        upload_api,
        filename="projection-replay.md",
        content=b"# Projection replay\n",
        operation_id=operation_id,
    )

    assert first.status_code == 503, first.text
    assert first.headers[UPLOAD_OPERATION_HEADER] == str(operation_id)
    assert first.headers["Retry-After"] == "1"
    assert replay.status_code == 200, replay.text
    assert replay.headers[UPLOAD_OPERATION_HEADER] == str(operation_id)
    with upload_api.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(LibraryItem)) == 1
        assert session.scalar(select(func.count()).select_from(LibraryItemVersion)) == 1
        assert (
            session.scalar(select(func.count()).select_from(MediaInstallRequest)) == 1
        )
        # LibraryRun is now shared by both stages (A-1 unification); the
        # upload flow also schedules preview_generation, which persists its
        # own run. Scope to stage="conversion" to preserve the original
        # intent (the replay did not create a duplicate conversion run).
        assert (
            session.scalar(
                select(func.count())
                .select_from(LibraryRun)
                .where(LibraryRun.stage == "conversion")
            )
            == 1
        )


def test_conversion_schedule_failure_preserves_downloadable_source(
    upload_api: UploadApi,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    services = upload_api.client.app.state.container.get_services()

    def fail_conversion_schedule(**_kwargs):
        raise RuntimeError("simulated optional conversion scheduler failure")

    monkeypatch.setattr(
        services.library_binary_registration,
        "schedule_conversion",
        fail_conversion_schedule,
    )
    uploaded = _upload(
        upload_api,
        filename="source-survives.md",
        content=b"# Source survives\n",
    )

    assert uploaded.status_code == 201, uploaded.text
    version = uploaded.json()["data"]["current_version"]
    assert version["processing_status"] == "stored_unconverted"
    downloaded = upload_api.client.get(version["original_url"])
    assert downloaded.status_code == 200, downloaded.text
    assert downloaded.content == b"# Source survives\n"
    with upload_api.session_factory() as session:
        assert session.get(LibraryItemVersion, UUID(version["id"])) is not None
        assert session.scalar(select(func.count()).select_from(StoredBlobRow)) == 1
        # LibraryRun is now shared by both stages (A-1 unification); the
        # upload flow separately (and unconditionally) attempts to schedule
        # preview_generation too, which persists its own run in the
        # "unavailable" state even with no provider chain -- that's not
        # "conversion was scheduled", so the assertion must scope to
        # stage="conversion" to preserve the original intent (no conversion
        # run/attempt exists because schedule_conversion was made to fail).
        assert (
            session.scalar(
                select(func.count())
                .select_from(LibraryRun)
                .where(LibraryRun.stage == "conversion")
            )
            == 0
        )


def test_preview_schedule_failure_does_not_rollback_conversion_job_or_source(
    upload_api: UploadApi,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    services = upload_api.client.app.state.container.get_services()

    def fail_preview_schedule(**_kwargs):
        raise RuntimeError("simulated optional Preview scheduler failure")

    monkeypatch.setattr(
        services.library_binary_registration,
        "schedule_preview",
        fail_preview_schedule,
    )
    uploaded = _upload(
        upload_api,
        filename="conversion-survives.md",
        content=b"# Conversion survives\n",
    )

    assert uploaded.status_code == 201, uploaded.text
    version = uploaded.json()["data"]["current_version"]
    assert version["processing_status"] == "queued"
    assert upload_api.client.get(version["original_url"]).status_code == 200
    with upload_api.session_factory() as session:
        assert (
            session.scalar(select(func.count()).select_from(LibraryRun)) == 1
        )
        assert (
            session.scalar(
                select(func.count())
                .select_from(Job)
                .where(Job.job_type == LIBRARY_CONVERSION_JOB_TYPE)
            )
            == 1
        )


def test_item_detail_projects_safe_retryable_conversion_failure(
    upload_api: UploadApi,
) -> None:
    uploaded = _upload(
        upload_api,
        filename="safe-failure.md",
        content=b"# Safe failure\n",
    )
    assert uploaded.status_code == 201, uploaded.text
    item_id = UUID(uploaded.json()["data"]["id"])
    version_id = UUID(uploaded.json()["data"]["current_version"]["id"])
    services = upload_api.client.app.state.container.get_services()
    with upload_api.session_factory.begin() as session:
        attempt = session.scalar(
            select(LibraryAttempt).where(
                LibraryAttempt.library_item_version_id == version_id,
                LibraryAttempt.stage == "conversion",
            )
        )
        assert attempt is not None
        services.library_conversion.mark_terminal_failure(
            session=session,
            workspace_id=upload_api.workspace_id,
            attempt_id=attempt.id,
            execution_generation=attempt.execution_generation,
            manual_retryable=True,
            safe_error_code="converter_process_failed",
        )

    detail = upload_api.client.get(
        f"/api/v1/workspaces/{upload_api.workspace_id}/library/items/{item_id}"
    )

    assert detail.status_code == 200, detail.text
    current = detail.json()["data"]["current_version"]
    assert current["processing_status"] == "failed"
    assert current["conversion"] == {
        "status": "failed",
        "reason": "document_conversion_failed",
        "retryable": True,
    }
    assert "converter_process_failed" not in detail.text


def _terminal_conversion_for_retry(
    upload_api: UploadApi,
    *,
    filename: str,
    job_status: str,
    safe_error_code: str = "converter_process_failed",
) -> tuple[UUID, UUID, UUID, UUID, UUID, int, str]:
    uploaded = _upload(
        upload_api,
        filename=filename,
        content=f"# {filename}\n".encode(),
    )
    assert uploaded.status_code == 201, uploaded.text
    item_id = UUID(uploaded.json()["data"]["id"])
    version_id = UUID(uploaded.json()["data"]["current_version"]["id"])
    services = upload_api.client.app.state.container.get_services()
    with upload_api.session_factory.begin() as session:
        attempt = session.scalar(
            select(LibraryAttempt).where(
                LibraryAttempt.library_item_version_id == version_id,
                LibraryAttempt.stage == "conversion",
            )
        )
        assert attempt is not None
        run = session.get(LibraryRun, attempt.run_id)
        assert run is not None
        job = session.scalar(
            select(Job).where(
                Job.workspace_id == upload_api.workspace_id,
                Job.job_type == LIBRARY_CONVERSION_JOB_TYPE,
                Job.idempotency_key == f"{attempt.id}:{attempt.execution_generation}",
            )
        )
        assert job is not None
        attempt.warnings_json = [{"code": "previous_warning"}]
        attempt.provenance_json = {"provider": "previous_generation"}
        attempt.metrics_json = {"elapsed_ms": 17}
        job.status = "running"
        job.stage = "conversion"
        job.started_at = job.created_at
        services.library_conversion.mark_terminal_failure(
            session=session,
            workspace_id=upload_api.workspace_id,
            attempt_id=attempt.id,
            execution_generation=attempt.execution_generation,
            manual_retryable=True,
            safe_error_code=safe_error_code,
        )
        job.progress = 67
        if job_status == "completed":
            JobQueueService().complete(session=session, job=job)
            job.retry_count = 2
            job.last_error = "previous worker error"
        elif job_status == "failed":
            JobQueueService().fail(
                session=session,
                job=job,
                error="previous worker error",
                max_retries=1,
            )
        elif job_status == "running":
            job.retry_count = 1
            job.last_error = "previous worker error"
        else:  # pragma: no cover - test helper misuse
            raise AssertionError(f"unsupported terminal status: {job_status}")
        assert job.last_error is not None
        return (
            item_id,
            version_id,
            run.id,
            attempt.id,
            job.id,
            job.retry_count,
            job.last_error,
        )


@pytest.mark.parametrize("job_status", ("completed", "failed", "running"))
def test_item_conversion_retry_requeues_same_generation_lineage_and_job(
    upload_api: UploadApi,
    job_status: str,
) -> None:
    (
        item_id,
        version_id,
        run_id,
        attempt_id,
        job_id,
        retry_count,
        last_error,
    ) = _terminal_conversion_for_retry(
        upload_api,
        filename=f"retry-{job_status}.md",
        job_status=job_status,
    )

    response = upload_api.client.post(
        f"/api/v1/workspaces/{upload_api.workspace_id}/library/versions/{version_id}/"
        f"conversion/retry"
    )

    assert response.status_code == 200, response.text
    current = response.json()["data"]["current_version"]
    assert current["id"] == str(version_id)
    assert current["processing_status"] == "queued"
    assert current["conversion"]["retryable"] is False
    assert "converter_process_failed" not in response.text
    assert "previous_warning" not in response.text
    assert "previous_generation" not in response.text
    with upload_api.session_factory() as session:
        attempt = session.get(LibraryAttempt, attempt_id)
        run = session.get(LibraryRun, run_id)
        version = session.get(LibraryItemVersion, version_id)
        job = session.get(Job, job_id)
        retry_audit = session.scalar(
            select(AuditEvent).where(
                AuditEvent.action == "library.conversion.retried",
                AuditEvent.target_id == attempt_id,
            )
        )
        assert attempt is not None
        assert attempt.status == "queued"
        assert attempt.execution_generation == 2
        assert attempt.transport_retry_count == 0
        assert attempt.started_at is None
        assert attempt.finished_at is None
        assert attempt.error_code is None
        assert attempt.safe_error_summary is None
        assert attempt.conversion_detail.manual_retry_disposition is None
        assert attempt.conversion_detail.disposition_reason is None
        assert attempt.warnings_json == []
        assert attempt.provenance_json == {}
        assert attempt.metrics_json == {}
        assert run is not None and run.status == "queued"
        assert run.finished_at is None
        assert version is not None and version.processing_status == "queued"
        assert job is not None and job.status == "queued"
        assert job.idempotency_key == f"{attempt_id}:2"
        assert job.target_ref["execution_generation"] == 2
        assert job.retry_count == retry_count
        assert job.last_error == last_error
        assert job.progress == 0
        assert job.stage is None
        assert job.started_at is None
        assert job.next_attempt_at is None
        assert job.completed_at is None
        assert retry_audit is not None
        assert retry_audit.retention_class == "sensitive"
        assert retry_audit.payload is not None
        assert retry_audit.payload["execution_generation"] == 2
        assert retry_audit.payload["previous_failure"] == {
            "attempt_status": "failed",
            "execution_generation": 1,
            "transport_retry_count": 0,
            "error_code": "converter_process_failed",
            "safe_error_summary": "The converter worker failed.",
            "manual_retry_disposition": "allowed",
            "disposition_reason": "error_retryable",
            "warnings_evidence": {
                "entry_count": 1,
                "sha256": hashlib.sha256(b'[{"code":"previous_warning"}]').hexdigest(),
            },
            "provenance_evidence": {
                "entry_count": 1,
                "sha256": hashlib.sha256(
                    b'{"provider":"previous_generation"}'
                ).hexdigest(),
            },
            "metrics_evidence": {
                "entry_count": 1,
                "sha256": hashlib.sha256(b'{"elapsed_ms":17}').hexdigest(),
            },
            "job_id": str(job_id),
            "job_status": job_status,
            "job_idempotency_key": f"{attempt_id}:1",
            "job_retry_count": retry_count,
        }


def test_item_conversion_retry_hydrates_detail_only_after_mutation(
    upload_api: UploadApi,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    item_id, version_id, _, _, _, _, _ = _terminal_conversion_for_retry(
        upload_api,
        filename="retry-single-detail-projection.md",
        job_status="failed",
    )
    services = upload_api.client.app.state.container.get_services()
    original_get = services.domain_read.get_library_item
    detail_projection_calls = 0

    def count_detail_projection(*args, **kwargs):
        nonlocal detail_projection_calls
        detail_projection_calls += 1
        return original_get(*args, **kwargs)

    monkeypatch.setattr(
        services.domain_read,
        "get_library_item",
        count_detail_projection,
    )

    response = upload_api.client.post(
        f"/api/v1/workspaces/{upload_api.workspace_id}/library/versions/{version_id}/"
        f"conversion/retry"
    )

    assert response.status_code == 200, response.text
    assert detail_projection_calls == 1


def test_item_conversion_retry_refuses_nonretryable_failure_without_mutation(
    upload_api: UploadApi,
) -> None:
    (
        item_id,
        version_id,
        run_id,
        attempt_id,
        job_id,
        _,
        _,
    ) = _terminal_conversion_for_retry(
        upload_api,
        filename="retry-denied.md",
        job_status="completed",
        safe_error_code="active_content_denied",
    )

    response = upload_api.client.post(
        f"/api/v1/workspaces/{upload_api.workspace_id}/library/versions/{version_id}/"
        f"conversion/retry"
    )

    assert response.status_code == 409, response.text
    assert response.json()["error_code"] == "library_conversion_retry_not_available"
    with upload_api.session_factory() as session:
        attempt = session.get(LibraryAttempt, attempt_id)
        run = session.get(LibraryRun, run_id)
        version = session.get(LibraryItemVersion, version_id)
        job = session.get(Job, job_id)
        assert attempt is not None and attempt.status == "failed"
        assert attempt.execution_generation == 1
        assert attempt.error_code == "active_content_denied"
        assert run is not None and run.status == "failed"
        assert version is not None and version.processing_status == "failed"
        assert job is not None and job.status == "completed"


def test_input_limit_failure_is_not_retryable_in_read_or_mutation(
    upload_api: UploadApi,
) -> None:
    (
        item_id,
        version_id,
        _,
        attempt_id,
        job_id,
        _,
        _,
    ) = _terminal_conversion_for_retry(
        upload_api,
        filename="retry-input-limit-denied.md",
        job_status="completed",
    )
    with upload_api.session_factory.begin() as session:
        attempt = session.get(LibraryAttempt, attempt_id)
        assert attempt is not None
        attempt.error_code = "input_limit_exceeded"
        attempt.safe_error_summary = "The source exceeds the converter input limit."
        attempt.conversion_detail.manual_retry_disposition = "denied"
        attempt.conversion_detail.disposition_reason = "error_not_retryable"

    detail = upload_api.client.get(
        f"/api/v1/workspaces/{upload_api.workspace_id}/library/items/{item_id}"
    )
    response = upload_api.client.post(
        f"/api/v1/workspaces/{upload_api.workspace_id}/library/versions/{version_id}/"
        f"conversion/retry"
    )

    assert detail.status_code == 200, detail.text
    assert detail.json()["data"]["current_version"]["conversion"] == {
        "status": "failed",
        "reason": "document_conversion_failed",
        "retryable": False,
    }
    assert "input_limit_exceeded" not in detail.text
    assert response.status_code == 409, response.text
    assert response.json()["error_code"] == "library_conversion_retry_not_available"
    with upload_api.session_factory() as session:
        attempt = session.get(LibraryAttempt, attempt_id)
        job = session.get(Job, job_id)
        assert attempt is not None
        assert attempt.status == "failed"
        assert attempt.execution_generation == 1
        assert attempt.conversion_detail.manual_retry_disposition == "denied"
        assert job is not None and job.status == "completed"


def test_item_conversion_retry_is_single_generation_for_duplicate_requests(
    upload_api: UploadApi,
) -> None:
    item_id, version_id, _, attempt_id, job_id, _, _ = _terminal_conversion_for_retry(
        upload_api,
        filename="retry-twice.md",
        job_status="failed",
    )
    path = (
        f"/api/v1/workspaces/{upload_api.workspace_id}/library/versions/{version_id}/"
        f"conversion/retry"
    )

    first = upload_api.client.post(path)
    second = upload_api.client.post(path)

    assert first.status_code == 200, first.text
    assert second.status_code == 409, second.text
    assert second.json()["error_code"] == "library_conversion_retry_not_available"
    with upload_api.session_factory() as session:
        attempt = session.get(LibraryAttempt, attempt_id)
        job = session.get(Job, job_id)
        assert attempt is not None and attempt.execution_generation == 2
        assert attempt.transport_retry_count == 0
        assert job is not None and job.target_ref["execution_generation"] == 2


def test_item_conversion_retry_rejects_stale_historical_version_without_mutation(
    upload_api: UploadApi,
) -> None:
    item_id, version_id, _, attempt_id, job_id, _, _ = _terminal_conversion_for_retry(
        upload_api,
        filename="retry-current-only.md",
        job_status="completed",
    )
    replacement = _add_version(
        upload_api,
        item_id=item_id,
        filename="retry-current-only.md",
        content=b"# New current version\n",
    )
    assert replacement.status_code == 201, replacement.text
    assert UUID(replacement.json()["data"]["id"]) == item_id
    assert UUID(replacement.json()["data"]["current_version"]["id"]) != version_id

    response = upload_api.client.post(
        f"/api/v1/workspaces/{upload_api.workspace_id}/library/versions/{version_id}/"
        f"conversion/retry"
    )

    assert response.status_code == 409, response.text
    assert response.json()["error_code"] == "library_conversion_retry_not_available"
    with upload_api.session_factory() as session:
        attempt = session.get(LibraryAttempt, attempt_id)
        job = session.get(Job, job_id)
        assert attempt is not None and attempt.status == "failed"
        assert attempt.execution_generation == 1
        assert attempt.transport_retry_count == 0
        assert job is not None and job.status == "completed"
        assert job.target_ref["execution_generation"] == 1


def test_item_conversion_retry_operates_only_on_the_targeted_version(
    upload_api: UploadApi,
) -> None:
    # P9.2: conversion/retry is flat (versions/{version_id}/conversion/retry,
    # no item in the URL), so a second item's own version is simply its own
    # legitimate retry target -- what must stay true is that retrying it
    # never mutates an unrelated item's own attempt/Job.
    first_item_id, _, _, first_attempt_id, first_job_id, _, _ = (
        _terminal_conversion_for_retry(
            upload_api,
            filename="retry-first-item.md",
            job_status="failed",
        )
    )
    _, second_version_id, _, second_attempt_id, second_job_id, _, _ = (
        _terminal_conversion_for_retry(
            upload_api,
            filename="retry-second-item.md",
            job_status="failed",
        )
    )

    response = upload_api.client.post(
        f"/api/v1/workspaces/{upload_api.workspace_id}/library/versions/{second_version_id}/"
        f"conversion/retry"
    )

    assert response.status_code == 200, response.text
    with upload_api.session_factory() as session:
        first_attempt = session.get(LibraryAttempt, first_attempt_id)
        first_job = session.get(Job, first_job_id)
        assert first_attempt is not None and first_attempt.execution_generation == 1
        assert first_job is not None and first_job.status == "failed"


def test_item_conversion_retry_hides_cross_workspace_item_without_mutation(
    upload_api: UploadApi,
) -> None:
    item_id, version_id, _, attempt_id, job_id, _, _ = _terminal_conversion_for_retry(
        upload_api,
        filename="retry-cross-workspace.md",
        job_status="failed",
    )
    with upload_api.session_factory.begin() as session:
        owner = session.scalar(
            select(User).where(User.email == "library-upload-p0-owner@example.com")
        )
        assert owner is not None
        foreign_workspace = Workspace(
            slug=f"retry-foreign-{uuid4().hex}",
            title="Retry foreign workspace",
            name="Retry foreign workspace",
        )
        session.add(foreign_workspace)
        session.flush()
        grant_workspace_membership(
            session,
            user_id=owner.id,
            workspace_id=foreign_workspace.id,
            role="owner",
        )
        foreign_workspace_id = foreign_workspace.id

    response = upload_api.client.post(
        f"/api/v1/workspaces/{foreign_workspace_id}/library/versions/{version_id}/"
        f"conversion/retry"
    )

    assert response.status_code == 404, response.text
    assert response.json()["error_code"] == "not_found"
    with upload_api.session_factory() as session:
        attempt = session.get(LibraryAttempt, attempt_id)
        job = session.get(Job, job_id)
        assert attempt is not None and attempt.execution_generation == 1
        assert job is not None and job.status == "failed"


@pytest.mark.parametrize("lifecycle_status", ("archived", "hidden"))
def test_item_conversion_retry_hides_excluded_item_before_state_conflict(
    upload_api: UploadApi,
    lifecycle_status: str,
) -> None:
    item_id, version_id, _, attempt_id, job_id, _, _ = _terminal_conversion_for_retry(
        upload_api,
        filename=f"retry-{lifecycle_status}.md",
        job_status="failed",
        safe_error_code="active_content_denied",
    )
    with upload_api.session_factory.begin() as session:
        item = session.get(LibraryItem, item_id)
        assert item is not None
        item.lifecycle_status = lifecycle_status

    response = upload_api.client.post(
        f"/api/v1/workspaces/{upload_api.workspace_id}/library/versions/{version_id}/"
        f"conversion/retry"
    )

    assert response.status_code == 404, response.text
    assert response.json()["error_code"] == "not_found"
    with upload_api.session_factory() as session:
        attempt = session.get(LibraryAttempt, attempt_id)
        job = session.get(Job, job_id)
        assert attempt is not None and attempt.execution_generation == 1
        assert attempt.status == "failed"
        assert attempt.error_code == "active_content_denied"
        assert job is not None and job.status == "failed"


def test_item_conversion_retry_denies_restricted_item_to_editor_without_mutation(
    upload_api: UploadApi,
) -> None:
    item_id, version_id, _, attempt_id, job_id, _, _ = _terminal_conversion_for_retry(
        upload_api,
        filename="retry-restricted.md",
        job_status="failed",
        safe_error_code="active_content_denied",
    )
    with upload_api.session_factory.begin() as session:
        item = session.get(LibraryItem, item_id)
        assert item is not None
        item.trust_level = "restricted"
    authenticate_test_client_in_workspace(
        upload_api.client,
        upload_api.session_factory,
        email="library-retry-restricted-editor@example.com",
        workspace_slug="default",
        role="editor",
    )

    response = upload_api.client.post(
        f"/api/v1/workspaces/{upload_api.workspace_id}/library/versions/{version_id}/"
        f"conversion/retry"
    )

    assert response.status_code == 403, response.text
    assert response.json()["error_code"] == "restricted_library_denied"
    with upload_api.session_factory() as session:
        attempt = session.get(LibraryAttempt, attempt_id)
        job = session.get(Job, job_id)
        assert attempt is not None and attempt.execution_generation == 1
        assert attempt.status == "failed"
        assert attempt.error_code == "active_content_denied"
        assert job is not None and job.status == "failed"


def test_item_conversion_retry_requires_editor_access(
    upload_api: UploadApi,
) -> None:
    item_id, version_id, _, attempt_id, _, _, _ = _terminal_conversion_for_retry(
        upload_api,
        filename="retry-editor-only.md",
        job_status="failed",
    )
    authenticate_test_client_in_workspace(
        upload_api.client,
        upload_api.session_factory,
        email="library-retry-viewer@example.com",
        workspace_slug="default",
        role="viewer",
    )

    response = upload_api.client.post(
        f"/api/v1/workspaces/{upload_api.workspace_id}/library/versions/{version_id}/"
        f"conversion/retry"
    )

    assert response.status_code == 403, response.text
    with upload_api.session_factory() as session:
        attempt = session.get(LibraryAttempt, attempt_id)
        assert attempt is not None and attempt.execution_generation == 1


def test_item_detail_projects_fail_closed_conversion_as_non_retryable(
    upload_api: UploadApi,
) -> None:
    uploaded = _upload(
        upload_api,
        filename="safe-timeout.md",
        content=b"# Safe timeout\n",
    )
    assert uploaded.status_code == 201, uploaded.text
    item_id = UUID(uploaded.json()["data"]["id"])
    version_id = UUID(uploaded.json()["data"]["current_version"]["id"])
    cases = (
        (
            "timed_out",
            "converter_timed_out",
            "document_conversion_timed_out",
        ),
        ("failed", "container_ratio_exceeded", "document_conversion_failed"),
        ("failed", "xml_depth_exceeded", "document_conversion_failed"),
    )
    for status, error_code, reason in cases:
        with upload_api.session_factory.begin() as session:
            attempt = session.scalar(
                select(LibraryAttempt).where(
                    LibraryAttempt.library_item_version_id == version_id,
                    LibraryAttempt.stage == "conversion",
                )
            )
            assert attempt is not None
            run = session.get(LibraryRun, attempt.run_id)
            version = session.get(LibraryItemVersion, version_id)
            assert run is not None
            assert version is not None
            attempt.status = status
            attempt.error_code = error_code
            attempt.safe_error_summary = "The converter stopped safely."
            run.status = status
            version.processing_status = status

        detail = upload_api.client.get(
            f"/api/v1/workspaces/{upload_api.workspace_id}/library/items/{item_id}"
        )

        assert detail.status_code == 200, detail.text
        current = detail.json()["data"]["current_version"]
        assert current["processing_status"] == status
        assert current["conversion"] == {
            "status": status,
            "reason": reason,
            "retryable": False,
        }
        assert error_code not in detail.text


def test_read_projection_never_exposes_raw_extraction_diagnostics(
    upload_api: UploadApi,
) -> None:
    uploaded = _upload(
        upload_api,
        filename="safe-summary.md",
        content=b"# Safe summary\n",
    )
    assert uploaded.status_code == 201, uploaded.text
    item_id = UUID(uploaded.json()["data"]["id"])
    version_id = UUID(uploaded.json()["data"]["current_version"]["id"])
    unsafe = r"converter failed at C:\private\source.docx: raw stderr"
    item_url = f"/api/v1/workspaces/{upload_api.workspace_id}/library/items/{item_id}"
    version_url = (
        f"/api/v1/workspaces/{upload_api.workspace_id}/library/versions/{version_id}"
    )

    for processing_status in (
        "failed",
        "timed_out",
        "queued",
        "stored_unconverted",
        "ready",
        "ready_partial",
    ):
        with upload_api.session_factory.begin() as session:
            version = session.get(LibraryItemVersion, version_id)
            assert version is not None
            version.processing_status = processing_status
            version.extraction_summary = unsafe

        detail = upload_api.client.get(item_url)
        assert detail.status_code == 200, detail.text
        payload = detail.json()["data"]
        assert payload["current_version"]["extraction_summary"] is None
        assert payload["versions"][0]["extraction_summary"] is None
        assert unsafe not in detail.text

    version_response = upload_api.client.get(version_url)
    assert version_response.status_code == 200, version_response.text
    assert version_response.json()["data"]["extraction_summary"] is None
    assert unsafe not in version_response.text

    with upload_api.session_factory.begin() as session:
        version = session.get(LibraryItemVersion, version_id)
        assert version is not None
        version.processing_status = "completed"
        version.extraction_summary = "extracted 15 characters"

    safe_success = upload_api.client.get(version_url)
    assert safe_success.status_code == 200, safe_success.text
    assert safe_success.json()["data"]["extraction_summary"] == (
        "extracted 15 characters"
    )

    with upload_api.session_factory.begin() as session:
        version = session.get(LibraryItemVersion, version_id)
        assert version is not None
        version.extraction_summary = unsafe

    forged_success = upload_api.client.get(version_url)
    assert forged_success.status_code == 200, forged_success.text
    assert forged_success.json()["data"]["extraction_summary"] is None
    assert unsafe not in forged_success.text

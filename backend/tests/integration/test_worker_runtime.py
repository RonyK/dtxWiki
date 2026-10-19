import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

from sqlalchemy import create_engine, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker
from typer.testing import CliRunner

from backend.app.bootstrap import ensure_data_dirs
from backend.app.container import DatabaseProvider
from backend.app.db import make_session_factory_from_settings
from backend.app.settings import Settings
from backend.media.adapters.sqlalchemy.reconcile import MediaInstallReconcileResult
from backend.models.audit import AuditEvent
from backend.models.base import Base
from backend.models.bundle import AgentInputBundle
from backend.models.job import Job, WorkspaceJobHint
from backend.models.library import LibraryArtifact, LibraryItem, LibraryItemVersion
from backend.models.library_discovery import LibraryDiscoveryRun
from backend.models.lint import LintRun
from backend.models.mail import EmailOutbox
from backend.models.user import User
from backend.models.workspace import MCPClient, Workspace, WorkspaceMembership
from backend.schemas.lint import LintRunCreateRequest
from backend.scripts.seed_dev import seed_default_workspace
from backend.services import job_hints
from backend.services.bundle_expiration import BundleExpirationService
from backend.services.context_media_policy import ContextMediaPolicy
from backend.services.credential_hashing import CredentialHasher
from backend.services.extraction_service import ExtractionService
from backend.services.job_hints import record_workspace_job_hint
from backend.services.job_queue import (
    DEFAULT_JOB_MAX_ATTEMPTS,
    JOB_TYPES_WITH_DOMAIN_FAILURE_STATE,
    JobQueueService,
)
from backend.services.library_service import LibraryService
from backend.services.lint_service import LintService
from backend.services.mail_delivery import (
    EmailDeliveryService,
    MailProviderError,
    OutboundMail,
    SMTPMailProvider,
)
from backend.services.password_recovery_service import PasswordRecoveryService
from backend.tests.utils.bootstrapped_sqlite import copy_migrated_sqlite
from backend.worker import entry as worker_entry
from backend.worker.entry import app as worker_cli
from backend.worker.registry import registered_handlers
from backend.worker.runtime import OneShotWorker
from backend.worker.tick import seed_due_jobs


def _prepare_database(database_url: str) -> None:
    """Leave an unseeded database at the head schema at ``database_url``.

    The same state ``bootstrap`` produces, from a copy of a once-built template:
    these tests drive the worker over jobs they seed themselves and do not test
    migration. The CLI invocations below still pass ``--skip-bootstrap`` as before.
    """

    ensure_data_dirs()
    copy_migrated_sqlite(Path(database_url.removeprefix("sqlite+pysqlite:///")))


def _clear_job_backoff(session_factory):
    """Elapse retry backoff so a requeued job is immediately claimable in tests
    (fail() now schedules next_attempt_at in the future)."""
    with session_factory.begin() as session:
        for job in session.scalars(select(Job)).all():
            job.next_attempt_at = None


def make_session_factory():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def test_worker_registry_roster_is_explicit():
    assert set(registered_handlers()) == {
        "library_discovery",
        "extraction",
        "library_conversion",
        "library_preview",
        "feedback_resolution",
        "bundle_expiration_sweep",
        "embed",
        "lint_tick",
        "trail_tick",
        "librarian_tick",
        "media_install_reconcile",
        "context_media_orphan_sweep",
        "credential_replica_reconcile",
        "media_blob_gc",
        "context_reconciliation_reminder",
        "email_delivery",
    }


def test_due_tick_seeder_is_idempotent_and_honors_disabled_intervals():
    session_factory = make_session_factory()
    now = datetime(2026, 7, 12, 3, 0, tzinfo=UTC)
    settings = Settings(
        lint_tick_interval_hours=24,
        trail_tick_interval_hours=0,
        librarian_tick_interval_days=0,
        # Automatic reclamation is off; hourly Context drain still runs.
        context_media_orphan_retention_hours=0,
    )
    with session_factory.begin() as session:
        workspace = seed_default_workspace(session)
        first = seed_due_jobs(session, settings, now=now)
        second = seed_due_jobs(session, settings, now=now)
        jobs = session.scalars(select(Job)).all()

    assert [job.job_type for job in first] == [
        "bundle_expiration_sweep",
        "media_install_reconcile",
        "credential_replica_reconcile",
        "lint_tick",
        "context_media_orphan_sweep",
        "media_blob_gc",
    ]
    assert second == []
    bundle_bucket = now.replace(second=0, microsecond=0).isoformat()
    bucket = int(now.timestamp()) // (24 * 3600)
    assert [(job.job_type, job.idempotency_key) for job in jobs] == [
        (
            "bundle_expiration_sweep",
            f"{workspace.id}:{bundle_bucket}",
        ),
        (
            "media_install_reconcile",
            f"{workspace.id}:{bundle_bucket}",
        ),
        (
            "credential_replica_reconcile",
            f"{workspace.id}:{bundle_bucket}",
        ),
        ("lint_tick", f"lint_tick:24h:{bucket}"),
        (
            "context_media_orphan_sweep",
            f"context_media_orphan_sweep:1h:{int(now.timestamp()) // 3600}:"
            + ContextMediaPolicy(None, 0).revision,
        ),
        ("media_blob_gc", f"media_blob_gc:1h:{int(now.timestamp()) // 3600}"),
    ]
    assert jobs[0].workspace_id == workspace.id


def test_due_tick_uses_configured_interval_and_ignores_manual_job_history():
    session_factory = make_session_factory()
    start = datetime(2026, 7, 12, 1, 0, tzinfo=UTC)
    settings = Settings(
        lint_tick_interval_hours=6,
        trail_tick_interval_hours=0,
        librarian_tick_interval_days=0,
        context_media_orphan_retention_hours=0,
    )
    queue = JobQueueService()
    with session_factory.begin() as session:
        workspace = seed_default_workspace(session)
        first = seed_due_jobs(session, settings, now=start, job_queue=queue)
        queue.enqueue(
            session=session,
            workspace_id=workspace.id,
            job_type="lint_tick",
            trigger_type="manual",
            idempotency_key="manual-lint-run",
            target_ref={},
        )
        second = seed_due_jobs(
            session,
            settings,
            now=start + timedelta(hours=6),
            job_queue=queue,
        )

    first_lint = next(job for job in first if job.job_type == "lint_tick")
    second_lint = next(job for job in second if job.job_type == "lint_tick")
    assert {job.job_type for job in first} == {
        "bundle_expiration_sweep",
        "media_install_reconcile",
        "credential_replica_reconcile",
        "lint_tick",
        "context_media_orphan_sweep",
        "media_blob_gc",
    }
    assert {job.job_type for job in second} == {
        "bundle_expiration_sweep",
        "media_install_reconcile",
        "credential_replica_reconcile",
        "lint_tick",
        "context_media_orphan_sweep",
        "media_blob_gc",
    }
    assert first_lint.idempotency_key != second_lint.idempotency_key
    assert second_lint.trigger_type == "scheduled"


def test_due_tick_seeds_minute_maintenance_jobs_once_per_minute():
    session_factory = make_session_factory()
    start = datetime(2026, 7, 30, 0, 59, 59, tzinfo=UTC)
    settings = Settings(
        lint_tick_interval_hours=0,
        trail_tick_interval_hours=0,
        librarian_tick_interval_days=0,
        context_media_orphan_retention_hours=0,
    )
    with session_factory.begin() as session:
        seed_default_workspace(session)
        first = seed_due_jobs(session, settings, now=start)
        same_hour = seed_due_jobs(
            session,
            settings,
            now=start,
        )
        next_hour = seed_due_jobs(
            session,
            settings,
            now=start + timedelta(seconds=1),
        )

    assert [job.job_type for job in first] == [
        "bundle_expiration_sweep",
        "media_install_reconcile",
        "credential_replica_reconcile",
        "context_media_orphan_sweep",
        "media_blob_gc",
    ]
    assert same_hour == []
    assert [job.job_type for job in next_hour] == [
        "bundle_expiration_sweep",
        "media_install_reconcile",
        "credential_replica_reconcile",
        "context_media_orphan_sweep",
        "media_blob_gc",
    ]


def test_due_tick_seeds_one_media_reconcile_job_for_every_workspace():
    session_factory = make_session_factory()
    scheduled_for = datetime(2026, 7, 30, 1, 2, 3, tzinfo=UTC)
    settings = Settings(
        lint_tick_interval_hours=0,
        trail_tick_interval_hours=0,
        librarian_tick_interval_days=0,
    )
    with session_factory.begin() as session:
        first_workspace = seed_default_workspace(session)
        second_workspace = Workspace(
            slug="media-maintenance-other",
            title="Media maintenance other",
            name="Media maintenance other",
        )
        session.add(second_workspace)
        session.flush()
        seeded = seed_due_jobs(session, settings, now=scheduled_for)
        same_bucket = seed_due_jobs(session, settings, now=scheduled_for)

    reconcile_jobs = [
        job for job in seeded if job.job_type == "media_install_reconcile"
    ]
    assert {job.workspace_id for job in reconcile_jobs} == {
        first_workspace.id,
        second_workspace.id,
    }
    assert len(reconcile_jobs) == 2
    assert len({job.idempotency_key for job in reconcile_jobs}) == 2
    assert all(job.trigger_type == "scheduled" for job in reconcile_jobs)
    assert same_bucket == []


def test_worker_dispatches_registered_media_install_reconciler():
    session_factory = make_session_factory()
    queue = JobQueueService()
    scheduled_for = datetime(2026, 7, 30, 1, 2, tzinfo=UTC)

    class _Reconciler:
        called = None

        def sweep_expired(self, session, *, workspace_id, now):
            assert session is not None
            self.called = (workspace_id, now)
            return MediaInstallReconcileResult(
                inspected_count=3,
                resumed_count=2,
                cleaned_count=1,
                deferred_count=0,
            )

    reconciler = _Reconciler()
    with session_factory.begin() as session:
        workspace = seed_default_workspace(session)
        queue.enqueue(
            session=session,
            workspace_id=workspace.id,
            job_type="media_install_reconcile",
            trigger_type="scheduled",
            idempotency_key="media-reconcile:test",
            target_ref={"scheduled_for": scheduled_for.isoformat()},
        )
        workspace_id = workspace.id

    worker = OneShotWorker(
        job_queue=queue,
        media_install_reconciler=reconciler,
    )
    with session_factory.begin() as session:
        result = worker.dispatch_once(
            session,
            workspace_id=workspace_id,
            job_types={"media_install_reconcile"},
        )

    assert result is not None
    assert result.status == "completed"
    assert result.detail == {
        "inspected_count": 3,
        "resumed_count": 2,
        "cleaned_count": 1,
        "deferred_count": 0,
    }
    assert reconciler.called == (workspace_id, scheduled_for)


def test_worker_run_once_does_not_seed_periodic_jobs(tmp_path, monkeypatch):
    database_url = f"sqlite+pysqlite:///{tmp_path / 'worker-once.db'}"
    _prepare_database(database_url)

    def _unexpected_seed(*_args, **_kwargs):
        raise AssertionError("run-once must not seed periodic jobs")

    monkeypatch.setattr(worker_entry, "_seed_due_jobs", _unexpected_seed)
    result = CliRunner().invoke(
        worker_cli,
        ["run-once", "--db", database_url, "--skip-bootstrap"],
    )

    assert result.exit_code == 0
    assert json.loads(result.stdout.strip()) == {"status": "idle"}


def test_worker_executes_queued_lint_run():
    session_factory = make_session_factory()
    queue = JobQueueService()
    worker = OneShotWorker(job_queue=queue)
    with session_factory.begin() as session:
        workspace = seed_default_workspace(session)
        queued = LintService(job_queue=queue).enqueue_run(
            session,
            workspace.id,
            LintRunCreateRequest(),
            actor_kind="human",
            actor_id="test-user",
        )
        assert queued.status == "queued"
        result = worker.dispatch_once(session, workspace_id=workspace.id)

    with session_factory() as session:
        run = session.get(LintRun, queued.id)
        job = session.scalar(select(Job).where(Job.job_type == "lint_tick"))

    assert result.status == "completed"
    assert run.status == "completed"
    assert run.started_at is not None
    assert run.completed_at is not None
    assert job.status == "completed"


def test_terminal_lint_worker_failure_marks_run_failed(monkeypatch):
    session_factory = make_session_factory()
    queue = JobQueueService()
    worker = OneShotWorker(job_queue=queue)

    def fail_lint(*_args, **_kwargs):
        raise RuntimeError("simulated lint failure")

    monkeypatch.setattr(LintService, "execute_queued_run", fail_lint)
    with session_factory.begin() as session:
        workspace = seed_default_workspace(session)
        queued = LintService(job_queue=queue).enqueue_run(
            session,
            workspace.id,
            LintRunCreateRequest(),
            actor_kind="human",
            actor_id="test-user",
        )

    result = None
    for _ in range(DEFAULT_JOB_MAX_ATTEMPTS):
        _clear_job_backoff(session_factory)
        result = worker.dispatch_once_with_session_factory(
            session_factory,
            workspace_id=workspace.id,
            job_types={"lint_tick"},
        )

    with session_factory() as session:
        run = session.get(LintRun, queued.id)
        job = session.scalar(select(Job).where(Job.job_type == "lint_tick"))

    assert result is not None and result.status == "failed"
    assert job.status == "failed"
    assert run.status == "failed"
    assert run.completed_at is not None


def test_completed_lint_run_reexecution_is_successful_noop(session_factory):
    with session_factory.begin() as session:
        workspace = seed_default_workspace(session)
        run = LintRun(
            workspace_id=workspace.id,
            lint_scope={"kind": "workspace"},
            trigger_type="manual",
            status="completed",
            finding_count_by_severity={"warning": 2},
            started_at=datetime(2026, 7, 12, tzinfo=UTC),
            completed_at=datetime(2026, 7, 12, 0, 1, tzinfo=UTC),
        )
        session.add(run)
        session.flush()

        repeated = LintService().execute_queued_run(
            session,
            workspace_id=workspace.id,
            run_id=run.id,
            trigger_type="manual",
        )

    assert repeated.status == "completed"
    assert repeated.finding_count_by_severity == {"warning": 2}


def test_worker_dispatches_library_discovery_job():
    session_factory = make_session_factory()
    queue = JobQueueService()
    worker = OneShotWorker(job_queue=queue)

    with session_factory.begin() as session:
        workspace = seed_default_workspace(session)
        workspace.library_policy = {
            **workspace.library_policy,
            "manual_approval_required": False,
        }
        discovery_job = queue.enqueue(
            session=session,
            workspace_id=workspace.id,
            job_type="library_discovery",
            trigger_type="manual",
            idempotency_key="worker-discovery-1",
            target_ref={
                "run_token": "worker-discovery-1",
                "candidates": [
                    {
                        "canonical_uri": "https://example.com/worker-source",
                        "title": "Worker Source",
                        "format_hint": "markdown",
                        "raw_text": "# Worker Source",
                    }
                ],
            },
        )
        result = worker.dispatch_once(session=session, workspace_id=workspace.id)

    with session_factory.begin() as session:
        job = session.get(Job, discovery_job.id)
        run = session.scalar(select(LibraryDiscoveryRun))
        source = session.scalar(select(LibraryItem))
        extraction_job = session.scalar(select(Job).where(Job.job_type == "extraction"))

    assert result.status == "completed"
    assert result.detail["candidate_count"] == 1
    assert result.detail["accepted_count"] == 1
    assert job.status == "completed"
    assert run.job_id == discovery_job.id
    assert source.uri == "https://example.com/worker-source"
    assert extraction_job.status == "queued"


def test_worker_dispatches_extraction_job():
    session_factory = make_session_factory()
    queue = JobQueueService()
    library_service = LibraryService(job_queue=queue)
    worker = OneShotWorker(job_queue=queue)

    with session_factory.begin() as session:
        workspace = seed_default_workspace(session)
        registration = library_service.register_library_item(
            session=session,
            workspace_id=workspace.id,
            canonical_uri="https://example.com/worker-extract",
            title="Worker Extract",
            raw_text="worker body",
        )
        result = worker.dispatch_once(session=session, workspace_id=workspace.id)

    with session_factory.begin() as session:
        job = session.get(Job, registration.extraction_job.id)
        artifact = session.scalar(select(LibraryArtifact))
        index_text = artifact.conversion_detail.index_text
        artifact_id = artifact.id
        job_status = job.status

    assert result.status == "completed"
    assert result.detail["library_artifact_id"] == str(artifact_id)
    assert job_status == "completed"
    assert index_text == "worker body"


def test_worker_dispatches_bundle_expiration_job():
    session_factory = make_session_factory()
    queue = JobQueueService()
    expiration_service = BundleExpirationService(job_queue=queue)
    worker = OneShotWorker(
        job_queue=queue,
        bundle_expiration_service=expiration_service,
    )
    now = datetime(2026, 5, 9, 16, 50, tzinfo=UTC)

    with session_factory.begin() as session:
        workspace = seed_default_workspace(session)
        client = MCPClient(
            workspace_id=workspace.id,
            name="worker-expiration-test-client",
            client_type="api_token",
            scopes=["mcp:read"],
            status="active",
        )
        session.add(client)
        session.flush()
        bundle = AgentInputBundle(
            workspace_id=workspace.id,
            client_id=client.id,
            task_intent="answer",
            token_budget=2048,
            ttl_seconds=3600,
            valid_until=now - timedelta(seconds=1),
        )
        session.add(bundle)
        sweep_job = expiration_service.enqueue_sweep(
            session=session,
            workspace_id=workspace.id,
            at=now,
        )
        result = worker.dispatch_once(session=session, workspace_id=workspace.id)

    with session_factory.begin() as session:
        job = session.get(Job, sweep_job.id)
        bundle = session.get(AgentInputBundle, bundle.id)

    assert result.status == "completed"
    assert result.detail["expired_count"] == 1
    assert job.status == "completed"
    assert bundle.status == "expired"


def test_worker_rejects_bundle_sweep_with_foreign_workspace_target_ref():
    """Fix-6 Phase 5 (Worker own-scope): the bundle-expiration dispatcher
    must reject a job whose target_ref points to a different workspace than
    the job row's own workspace_id. Pre-fix, target_ref["workspace_id"] was
    allowed to override `job.workspace_id` via the `_uuid_target` default
    fallback, letting a bundle sweep cross workspace boundaries even
    though the queue row tracked workspace A.

    The fix enforces job.target_ref["workspace_id"] == job.workspace_id
    when both are present, so the worker can only sweep its own
    workspace.
    """
    session_factory = make_session_factory()
    queue = JobQueueService()
    expiration_service = BundleExpirationService(job_queue=queue)
    worker = OneShotWorker(
        job_queue=queue,
        bundle_expiration_service=expiration_service,
    )
    now = datetime(2026, 5, 16, 12, 0, tzinfo=UTC)

    with session_factory.begin() as session:
        own_workspace = seed_default_workspace(session)
        foreign_workspace = seed_default_workspace(
            session,
            slug="fix6-worker-foreign",
            title="Fix-6 Worker Foreign",
        )
        sweep_job = expiration_service.enqueue_sweep(
            session=session,
            workspace_id=own_workspace.id,
            at=now,
        )
        # Tamper the queued job's target_ref to point to a foreign workspace.
        stored_job = session.get(Job, sweep_job.id)
        stored_job.target_ref = {
            **stored_job.target_ref,
            "workspace_id": str(foreign_workspace.id),
        }
        result = worker.dispatch_once(
            session=session,
            workspace_id=own_workspace.id,
        )

    with session_factory.begin() as session:
        final_job = session.get(Job, sweep_job.id)

    assert result.status == "failed"
    assert "does not match job.workspace_id" in result.detail["error"]
    assert final_job.status == "failed"


def test_job_types_with_domain_failure_state_matches_registry():
    """job_queue.JOB_TYPES_WITH_DOMAIN_FAILURE_STATE (which manual_retry uses
    to refuse a generic requeue) must name EXACTLY the job types whose
    registered handler writes a domain-specific on_failure marker -- neither
    a stale extra entry (which would wrongly block a safe retry) nor a
    missing one (which would let manual_retry resume a business handler
    against domain rows the on_failure marker already left terminal)."""
    handlers = registered_handlers()
    types_with_on_failure = {
        job_type
        for job_type, handler in handlers.items()
        if handler.on_failure is not None
    }
    assert JOB_TYPES_WITH_DOMAIN_FAILURE_STATE == types_with_on_failure


def _dispatch_failing_library_conversion(
    error: Exception,
    *,
    attempts: int,
) -> tuple[list[str], list[bool]]:
    session_factory = make_session_factory()
    queue = JobQueueService()
    recorded_retryability: list[bool] = []

    class _FailingConversionService:
        durable_media_installer_factory = None

        @staticmethod
        def mark_attempt_running(**_kwargs) -> bool:
            return True

        @staticmethod
        def execute_attempt(**_kwargs):
            raise error

        @staticmethod
        def mark_terminal_failure(*, manual_retryable: bool, **_kwargs) -> None:
            recorded_retryability.append(manual_retryable)

    worker = OneShotWorker(job_queue=queue)
    worker.library_conversion_service = _FailingConversionService()
    with session_factory.begin() as session:
        workspace = seed_default_workspace(session)
        queue.enqueue(
            session=session,
            workspace_id=workspace.id,
            job_type="library_conversion",
            trigger_type="manual",
            idempotency_key="conversion-failure-disposition",
            target_ref={
                "library_conversion_attempt_id": str(uuid4()),
                "execution_generation": 1,
            },
        )
        workspace_id = workspace.id

    statuses: list[str] = []
    for _ in range(attempts):
        _clear_job_backoff(session_factory)
        result = worker.dispatch_once_with_session_factory(
            session_factory=session_factory,
            workspace_id=workspace_id,
            job_types={"library_conversion"},
        )
        assert result is not None
        statuses.append(result.status)
    return statuses, recorded_retryability


def test_terminal_conversion_worker_error_persists_denied_manual_retry() -> None:
    statuses, retryability = _dispatch_failing_library_conversion(
        ValueError("malformed conversion job"),
        attempts=1,
    )

    assert statuses == ["failed"]
    assert retryability == [False]


def test_generic_conversion_worker_error_passes_retryable_failure_marker() -> None:
    statuses, retryability = _dispatch_failing_library_conversion(
        RuntimeError("temporary conversion worker failure"),
        attempts=1,
    )

    # This fake has no domain attempt, so queue/domain reconciliation closes
    # the Job immediately. The assertion here is the catch-path provenance:
    # generic worker failures reach the Library marker as retryable, whereas
    # the terminal-error test above reaches it as denied.
    assert statuses == ["failed"]
    assert retryability == [True]


def test_worker_marks_unsupported_job_failed():
    session_factory = make_session_factory()
    queue = JobQueueService()
    worker = OneShotWorker(job_queue=queue)

    with session_factory.begin() as session:
        workspace = seed_default_workspace(session)
        job = queue.enqueue(
            session=session,
            workspace_id=workspace.id,
            job_type="unknown",
            trigger_type="manual",
            idempotency_key="unknown-1",
        )
        result = worker.dispatch_once(session=session, workspace_id=workspace.id)

    with session_factory.begin() as session:
        stored_job = session.get(Job, job.id)

    assert result.status == "failed"
    assert result.detail["error"] == "unsupported worker job type: unknown"
    assert result.detail["terminal"] is True
    assert stored_job.status == "failed"
    assert stored_job.retry_count == 1


def test_dispatch_once_terminal_failure_queues_owner_notification_email():
    """Wiring check for _notify_terminal_failure (single-phase dispatch,
    worker/runtime.py): a job that dies terminally must leave a queued
    email_outbox row for the workspace owner, not just a failed Job row."""
    session_factory = make_session_factory()
    queue = JobQueueService()
    mail_settings = Settings(
        public_base_url="https://wiki.example.com",
        session_secret="terminal-notify-test-secret-with-entropy",
        mail_provider="smtp",
        mail_from_address="wiki@example.com",
        smtp_host="smtp.example.com",
    )
    worker = OneShotWorker(job_queue=queue, settings=mail_settings)

    with session_factory.begin() as session:
        workspace = seed_default_workspace(session)
        owner = User(
            email="owner@example.com",
            password_hash=CredentialHasher().hash_password("owner-password"),
            display_name="Owner",
            status="active",
            email_verified_at=datetime.now(UTC),
        )
        session.add(owner)
        session.flush()
        session.add(
            WorkspaceMembership(
                user_id=owner.id, workspace_id=workspace.id, role="owner"
            )
        )
        job = queue.enqueue(
            session=session,
            workspace_id=workspace.id,
            job_type="unknown",
            trigger_type="manual",
            idempotency_key="notify-unknown-1",
        )
        worker.dispatch_once(session=session, workspace_id=workspace.id)

    with session_factory.begin() as session:
        stored_job = session.get(Job, job.id)
        outbox = session.scalar(
            select(EmailOutbox).where(EmailOutbox.message_kind == "job_failed")
        )

    assert stored_job.status == "failed"
    assert outbox is not None
    assert outbox.recipient_email == "owner@example.com"
    assert outbox.source_job_type == "unknown"
    assert outbox.status == "queued"


def test_dispatch_once_records_failure_when_business_error_poisons_session():
    """Legacy single-phase dispatch: a business failure that poisons the
    shared transaction (IntegrityError at flush) must not escape as
    PendingRollbackError or roll back the claim. The business handler runs
    in a savepoint, so the failure bookkeeping still advances retry_count
    each round until the job fails terminally - no poison-message loop."""
    session_factory = make_session_factory()
    queue = JobQueueService()
    library_service = LibraryService(job_queue=queue)

    with session_factory.begin() as session:
        workspace = seed_default_workspace(session)
        registration = library_service.register_library_item(
            session=session,
            workspace_id=workspace.id,
            canonical_uri="https://example.com/poisoned-extract",
            title="Poisoned Extract",
            raw_text="body",
        )
        job_id = registration.extraction_job.id
        version_id = registration.version.id
        workspace_id = workspace.id
        idempotency_key = registration.extraction_job.idempotency_key

    class _PoisonExtractionService(ExtractionService):
        def extract_library_item_version(
            self, session, library_item_version_id, **kwargs
        ):
            # Violate the job idempotency unique constraint at flush time,
            # poisoning the transaction the claim also lives in.
            session.add(
                Job(
                    workspace_id=workspace_id,
                    job_type="extraction",
                    trigger_type="manual",
                    idempotency_key=idempotency_key,
                )
            )
            session.flush()
            raise AssertionError("flush should have raised IntegrityError")

    worker = OneShotWorker(
        job_queue=queue,
        extraction_service=_PoisonExtractionService(
            job_queue=queue, media_installer_factory=lambda session, workspace_id: None
        ),
    )

    statuses = []
    for _ in range(DEFAULT_JOB_MAX_ATTEMPTS):
        _clear_job_backoff(session_factory)
        with session_factory.begin() as session:
            result = worker.dispatch_once(
                session=session,
                workspace_id=workspace_id,
                job_types={"extraction"},
            )
        assert result is not None
        statuses.append(result.status)

    with session_factory.begin() as session:
        stored_job = session.get(Job, job_id)
        version = session.get(LibraryItemVersion, version_id)

    assert statuses == ["queued", "queued", "failed"]
    assert stored_job.status == "failed"
    assert stored_job.retry_count == DEFAULT_JOB_MAX_ATTEMPTS
    assert "UNIQUE constraint failed" in stored_job.last_error
    # The terminal failure re-applied the extraction domain markers.
    assert version.processing_status == "failed"


def test_worker_run_cli_processes_extraction_job(tmp_path):
    database_url = f"sqlite+pysqlite:///{tmp_path / 'worker-run.db'}"
    settings = Settings(database_url=database_url)
    _prepare_database(database_url)
    session_factory = make_session_factory_from_settings(settings)

    with session_factory.begin() as session:
        workspace = seed_default_workspace(session)
        registration = LibraryService().register_library_item(
            session=session,
            workspace_id=workspace.id,
            canonical_uri="https://example.com/worker-run",
            title="Worker Run",
            raw_text="worker run body",
        )

    result = CliRunner().invoke(
        worker_cli,
        [
            "run",
            "--db",
            database_url,
            "--job-type",
            "extraction",
            "--max-jobs",
            "1",
            "--poll-interval-seconds",
            "0",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout.strip())
    assert payload["job_type"] == "extraction"
    assert payload["status"] == "completed"

    with session_factory.begin() as session:
        job = session.get(Job, registration.extraction_job.id)
        artifact = session.scalar(select(LibraryArtifact))
        index_text = artifact.conversion_detail.index_text
        job_status = job.status

    assert job_status == "completed"
    assert index_text == "worker run body"


# ---------------------------------------------------------------------------
# BIZ-H-4: two-phase dispatch isolates job state transitions from the
# business transaction. The tests below pin the new behavior:
#   1. If phase-1 (business) raises, phase-2 still marks the job "failed"
#      because the fail update is its own transaction.
#   2. On success, business writes AND the "completed" status are persisted.
#   3. If phase-2 (complete) blows up at commit time, phase-1 writes are
#      still durable (we did NOT roll the business work back).
# ---------------------------------------------------------------------------


def test_business_failure_requeues_then_fails_after_attempt_budget(monkeypatch):
    """BIZ-H-4: a phase-1 (business) failure must not roll back the phase-2
    fail() status update. Generic failures are treated as transient: the job
    is requeued until the DEFAULT_JOB_MAX_ATTEMPTS budget (max total
    attempts) is exhausted, then failed terminally."""
    session_factory = make_session_factory()
    queue = JobQueueService()

    class _BoomExtractionService(ExtractionService):
        def extract_library_item_version(
            self, session, library_item_version_id, **kwargs
        ):
            # Simulate business work that touches the session and then blows
            # up. Phase 1's transaction should roll back; phase 2 should
            # still record the failure in a separate transaction.
            raise RuntimeError("simulated business explosion")

    boom = _BoomExtractionService(
        job_queue=queue, media_installer_factory=lambda session, workspace_id: None
    )
    worker = OneShotWorker(job_queue=queue, extraction_service=boom)
    library_service = LibraryService(job_queue=queue)

    with session_factory.begin() as session:
        workspace = seed_default_workspace(session)
        registration = library_service.register_library_item(
            session=session,
            workspace_id=workspace.id,
            canonical_uri="https://example.com/biz-h-4-fail",
            title="BIZ-H-4 Fail",
            raw_text="body",
        )
        job_id = registration.extraction_job.id

    results = []
    for _ in range(DEFAULT_JOB_MAX_ATTEMPTS):
        _clear_job_backoff(session_factory)
        results.append(
            worker.dispatch_once_with_session_factory(
                session_factory=session_factory,
                workspace_id=registration.library_item.workspace_id,
                job_types={"extraction"},
            )
        )

    with session_factory.begin() as session:
        stored_job = session.get(Job, job_id)

    assert all(result is not None for result in results)
    assert all(
        result.detail["error"] == "simulated business explosion" for result in results
    )
    assert [result.status for result in results] == ["queued", "queued", "failed"]
    assert stored_job is not None
    assert stored_job.status == "failed"
    assert stored_job.retry_count == DEFAULT_JOB_MAX_ATTEMPTS
    assert stored_job.last_error == "simulated business explosion"


def test_mark_failed_isolated_rejects_foreign_workspace_job_without_mutation():
    session_factory = make_session_factory()
    queue = JobQueueService()
    worker = OneShotWorker(job_queue=queue)

    with session_factory.begin() as session:
        workspace = seed_default_workspace(session)
        foreign_workspace = Workspace(
            slug="worker-foreign-failure-job",
            title="Worker foreign failure job",
            name="Worker foreign failure job",
        )
        session.add(foreign_workspace)
        session.flush()
        foreign_job = queue.enqueue(
            session=session,
            workspace_id=foreign_workspace.id,
            job_type="extraction",
            trigger_type="manual",
            idempotency_key="worker-foreign-failure-job",
        )
        foreign_job.status = "running"
        workspace_id = workspace.id
        foreign_job_id = foreign_job.id

    status = worker._mark_failed_isolated(
        session_factory=session_factory,
        workspace_id=workspace_id,
        job_id=foreign_job_id,
        error="forged cross-workspace failure",
        max_retries=0,
    )

    with session_factory() as session:
        stored_foreign_job = session.get(Job, foreign_job_id)

    assert status == "unknown"
    assert stored_foreign_job is not None
    assert stored_foreign_job.status == "running"
    assert stored_foreign_job.retry_count == 0
    assert stored_foreign_job.last_error is None


def test_two_phase_extraction_failure_persists_domain_failure_markers():
    """A phase-1 extraction failure rolls back the business transaction,
    including the version/source failure markers and the failure audit event
    the service wrote inside it. While retry budget remains the job is only
    requeued (no markers yet); once it is terminally failed, the orchestrator
    must re-apply those markers in an isolated transaction so the library
    item does not stay 'extract_pending' forever."""
    session_factory = make_session_factory()
    queue = JobQueueService()
    worker = OneShotWorker(job_queue=queue)
    library_service = LibraryService(job_queue=queue)

    with session_factory.begin() as session:
        workspace = seed_default_workspace(session)
        registration = library_service.register_library_item(
            session=session,
            workspace_id=workspace.id,
            canonical_uri="https://example.com/two-phase-extract-fail",
            title="Two Phase Extract Fail",
            raw_storage_ref="missing://raw",
        )
        job_id = registration.extraction_job.id
        version_id = registration.version.id
        item_id = registration.library_item.id
        workspace_id = workspace.id

    first = worker.dispatch_once_with_session_factory(
        session_factory=session_factory,
        workspace_id=workspace_id,
        job_types={"extraction"},
    )

    with session_factory.begin() as session:
        interim_version = session.get(LibraryItemVersion, version_id)
        assert first is not None
        assert first.status == "queued"
        # Markers wait until the job is terminally failed.
        assert interim_version.processing_status != "failed"

    result = first
    for _ in range(DEFAULT_JOB_MAX_ATTEMPTS - 1):
        _clear_job_backoff(session_factory)
        result = worker.dispatch_once_with_session_factory(
            session_factory=session_factory,
            workspace_id=workspace_id,
            job_types={"extraction"},
        )

    with session_factory.begin() as session:
        stored_job = session.get(Job, job_id)
        version = session.get(LibraryItemVersion, version_id)
        source = session.get(LibraryItem, item_id)
        failed_audit = session.scalar(
            select(AuditEvent).where(AuditEvent.action == "library.extraction.failed")
        )

    assert result is not None
    assert result.status == "failed"
    assert stored_job.status == "failed"
    assert version.processing_status == "failed"
    assert version.extraction_summary == "unsupported source storage ref: missing://raw"
    assert source.lifecycle_status == "extract_failed"
    assert failed_audit is not None
    assert failed_audit.target_id == version_id


def test_two_phase_failure_marker_errors_do_not_mask_job_failure(monkeypatch):
    """If persisting the domain failure markers itself blows up, the job must
    still be reported and recorded as failed - marker writes are best-effort
    and may never mask the dispatch outcome."""
    session_factory = make_session_factory()
    queue = JobQueueService()
    worker = OneShotWorker(job_queue=queue)
    library_service = LibraryService(job_queue=queue)

    with session_factory.begin() as session:
        workspace = seed_default_workspace(session)
        registration = library_service.register_library_item(
            session=session,
            workspace_id=workspace.id,
            canonical_uri="https://example.com/marker-boom",
            title="Marker Boom",
            raw_storage_ref="missing://raw",
        )
        job_id = registration.extraction_job.id
        workspace_id = workspace.id

    def _boom_marker(*args, **kwargs):
        raise RuntimeError("simulated marker write failure")

    monkeypatch.setattr(worker.extraction_service, "mark_version_failed", _boom_marker)

    result = None
    for _ in range(DEFAULT_JOB_MAX_ATTEMPTS):
        _clear_job_backoff(session_factory)
        result = worker.dispatch_once_with_session_factory(
            session_factory=session_factory,
            workspace_id=workspace_id,
            job_types={"extraction"},
        )

    with session_factory.begin() as session:
        stored_job = session.get(Job, job_id)

    assert result is not None
    assert result.status == "failed"
    assert stored_job.status == "failed"
    assert "unsupported source storage ref" in stored_job.last_error


def test_business_success_commits_then_marks_complete():
    """BIZ-H-4: on success, both the business rows and the job "completed"
    status are persisted to the DB. The two-phase dispatcher must commit
    phase 1 (business writes) before opening phase 2 (status update)."""
    session_factory = make_session_factory()
    queue = JobQueueService()
    worker = OneShotWorker(job_queue=queue)
    library_service = LibraryService(job_queue=queue)

    with session_factory.begin() as session:
        workspace = seed_default_workspace(session)
        registration = library_service.register_library_item(
            session=session,
            workspace_id=workspace.id,
            canonical_uri="https://example.com/biz-h-4-ok",
            title="BIZ-H-4 OK",
            raw_text="happy body",
        )
        job_id = registration.extraction_job.id
        workspace_id = workspace.id

    result = worker.dispatch_once_with_session_factory(
        session_factory=session_factory,
        workspace_id=workspace_id,
        job_types={"extraction"},
    )

    with session_factory.begin() as session:
        stored_job = session.get(Job, job_id)
        artifact = session.scalar(select(LibraryArtifact))
        index_text = artifact.conversion_detail.index_text if artifact else None

    assert result is not None
    assert result.status == "completed"
    assert stored_job.status == "completed"
    # Phase 1 business writes are durable.
    assert artifact is not None
    assert index_text == "happy body"


def test_complete_commit_failure_preserves_business_writes(monkeypatch):
    """BIZ-H-4: if phase-2 (mark complete) fails at commit time, the phase-1
    business writes must still be durable. Pre-fix, both phases were in one
    outer transaction, so a status-update failure rolled back the business
    work too, causing re-execution of already-applied side effects."""
    session_factory = make_session_factory()
    queue = JobQueueService()
    worker = OneShotWorker(job_queue=queue)
    library_service = LibraryService(job_queue=queue)

    with session_factory.begin() as session:
        workspace = seed_default_workspace(session)
        registration = library_service.register_library_item(
            session=session,
            workspace_id=workspace.id,
            canonical_uri="https://example.com/biz-h-4-phase2-boom",
            title="BIZ-H-4 Phase2 Boom",
            raw_text="durable body",
        )
        job_id = registration.extraction_job.id
        workspace_id = workspace.id

    # Sabotage phase 2: every call to queue.complete raises. Phase 1's
    # business writes must still be committed to the DB.
    def _boom_complete(*args, **kwargs):
        raise RuntimeError("simulated status-update commit failure")

    monkeypatch.setattr(worker.job_queue, "complete", _boom_complete)

    result = worker.dispatch_once_with_session_factory(
        session_factory=session_factory,
        workspace_id=workspace_id,
        job_types={"extraction"},
    )

    with session_factory.begin() as session:
        stored_job = session.get(Job, job_id)
        artifact = session.scalar(select(LibraryArtifact))
        index_text = artifact.conversion_detail.index_text if artifact else None

    assert result is not None
    # Phase 1 writes survived even though phase 2 failed.
    assert artifact is not None
    assert index_text == "durable body"
    # The worker surfaces the partial-success state for reconciliation
    # instead of pretending the job completed cleanly.
    assert result.status == "completed_pending_status"
    # Job row stays "running" - phase 2 never committed - so a reconciler
    # can detect and finish the transition.
    assert stored_job is not None
    assert stored_job.status == "running"


def test_worker_reclaims_stale_running_job_via_session_factory():
    session_factory = make_session_factory()
    queue = JobQueueService()
    worker = OneShotWorker(job_queue=queue)

    with session_factory.begin() as session:
        workspace = seed_default_workspace(session)
        job = queue.enqueue(
            session=session,
            workspace_id=workspace.id,
            job_type="extraction",
            trigger_type="manual",
            idempotency_key="worker-reclaim-stale-1",
        )
        claimed = queue.claim_next(session=session, workspace_id=workspace.id)
        assert claimed.id == job.id
        claimed.started_at = datetime.now(UTC) - timedelta(seconds=1200)
        session.add(claimed)

    reclaimed_count = worker.reclaim_stale_jobs_with_session_factory(
        session_factory=session_factory,
        lease_timeout_seconds=600,
    )

    with session_factory.begin() as session:
        stored = session.get(Job, job.id)

    assert reclaimed_count == 1
    assert stored.status == "queued"
    assert stored.retry_count == 1
    assert stored.started_at is None


def test_worker_reclaim_with_session_factory_swallows_reclaim_errors(monkeypatch):
    """A workspace seeded here so the monkeypatched boom is actually reached
    -- with an empty roster reclaim_stale_jobs is never called at all, which
    would make this pass for the wrong reason (nothing to swallow, not
    "swallowed successfully")."""
    session_factory = make_session_factory()
    queue = JobQueueService()
    worker = OneShotWorker(job_queue=queue)

    with session_factory.begin() as session:
        seed_default_workspace(session)

    def _boom_reclaim(*args, **kwargs):
        raise RuntimeError("simulated reclaim failure")

    monkeypatch.setattr(worker.job_queue, "reclaim_stale_jobs", _boom_reclaim)

    reclaimed_count = worker.reclaim_stale_jobs_with_session_factory(
        session_factory=session_factory,
        lease_timeout_seconds=600,
    )

    assert reclaimed_count == 0


def test_reclaim_stale_jobs_fans_out_and_reclaims_every_workspace():
    """Phase 6a (plan §3 D-2 point 1): the pre-fan-out reclaim_stale_jobs had
    no workspace filter and scanned every 'running' row in one query, which
    is exactly how a crashed worker's jobs went unreclaimed forever once the
    DB is sharded per workspace -- silently, with nothing to notice. This is
    the silent-loss case: two workspaces each with a stale job, and the fan-
    out must reclaim BOTH, not just whichever workspace happens to be first
    (which a fan-out that only reads the roster but forgets to actually loop
    over it, or that only reclaims workspace[0], would still pass a
    single-workspace test but fail here)."""
    session_factory = make_session_factory()
    queue = JobQueueService()
    worker = OneShotWorker(job_queue=queue)

    with session_factory.begin() as session:
        workspace_a = seed_default_workspace(session)
        workspace_b = Workspace(
            slug="reclaim-fanout-b",
            title="Reclaim Fanout B",
            name="Reclaim Fanout B",
        )
        session.add(workspace_b)
        session.flush()
        job_ids: dict[UUID, UUID] = {}
        for workspace in (workspace_a, workspace_b):
            job = queue.enqueue(
                session=session,
                workspace_id=workspace.id,
                job_type="extraction",
                trigger_type="manual",
                idempotency_key=f"reclaim-fanout-{workspace.slug}",
            )
            claimed = queue.claim_next(session=session, workspace_id=workspace.id)
            assert claimed.id == job.id
            claimed.started_at = datetime.now(UTC) - timedelta(seconds=1200)
            session.add(claimed)
            job_ids[workspace.id] = job.id

    reclaimed_count = worker.reclaim_stale_jobs_with_session_factory(
        session_factory=session_factory,
        lease_timeout_seconds=600,
    )

    with session_factory.begin() as session:
        statuses = {
            workspace_id: session.get(Job, job_id).status
            for workspace_id, job_id in job_ids.items()
        }

    assert reclaimed_count == 2
    assert set(statuses.values()) == {"queued"}


def test_reclaim_unreachable_workspace_does_not_abort_the_rest_of_the_sweep(
    monkeypatch,
):
    """D-2's "workspace unreachable mid-sweep" concern: one workspace's
    reclaim raising must not take the whole sweep down with it. A healthy
    workspace's stale job is still reclaimed even though the other
    workspace's reclaim blows up every time."""
    session_factory = make_session_factory()
    queue = JobQueueService()
    worker = OneShotWorker(job_queue=queue)

    with session_factory.begin() as session:
        broken_workspace = seed_default_workspace(session)
        healthy_workspace = Workspace(
            slug="reclaim-healthy",
            title="Reclaim Healthy",
            name="Reclaim Healthy",
        )
        session.add(healthy_workspace)
        session.flush()
        job_ids: dict[UUID, UUID] = {}
        for workspace in (broken_workspace, healthy_workspace):
            job = queue.enqueue(
                session=session,
                workspace_id=workspace.id,
                job_type="extraction",
                trigger_type="manual",
                idempotency_key=f"reclaim-unreachable-{workspace.slug}",
            )
            claimed = queue.claim_next(session=session, workspace_id=workspace.id)
            assert claimed.id == job.id
            claimed.started_at = datetime.now(UTC) - timedelta(seconds=1200)
            session.add(claimed)
            job_ids[workspace.id] = job.id

    real_reclaim = queue.reclaim_stale_jobs

    def _reclaim_or_boom(*, session, workspace_id, lease_timeout_seconds, **kwargs):
        if workspace_id == broken_workspace.id:
            raise RuntimeError("simulated unreachable workspace DB")
        return real_reclaim(
            session=session,
            workspace_id=workspace_id,
            lease_timeout_seconds=lease_timeout_seconds,
            **kwargs,
        )

    monkeypatch.setattr(worker.job_queue, "reclaim_stale_jobs", _reclaim_or_boom)

    reclaimed_count = worker.reclaim_stale_jobs_with_session_factory(
        session_factory=session_factory,
        lease_timeout_seconds=600,
    )

    with session_factory.begin() as session:
        broken_status = session.get(Job, job_ids[broken_workspace.id]).status
        healthy_status = session.get(Job, job_ids[healthy_workspace.id]).status

    assert reclaimed_count == 1
    # The broken workspace's stale job is untouched -- not lost, just
    # deferred to the next sweep -- while the healthy one still got reclaimed.
    assert broken_status == "running"
    assert healthy_status == "queued"


def test_round_robin_claim_does_not_starve_a_workspace_behind_a_continuously_busy_one(
    tmp_path,
):
    """Phase 6a (plan §3 D-2 point 2): the natural way to write this loop is
    drain-then-advance -- keep claiming from a workspace while it still has
    work, only move to the next once it's empty. That lets a continuously-
    busy workspace starve everything behind it indefinitely.

    This pins the alternative directly: the busy workspace here has FIVE
    jobs queued, deliberately more than the two calls this test makes.
    Round robin's one-attempt-per-workspace-per-sweep, plus a cursor that
    persists across calls, means the quiet workspace's single job is claimed
    within those two calls (there are only two workspaces) -- not after
    busy's backlog drains. A drain-then-advance implementation started on
    the busy workspace would still be working through its backlog after two
    calls and would never have reached quiet at all.
    """
    database_url = f"sqlite+pysqlite:///{tmp_path / 'round-robin.db'}"
    settings = Settings(database_url=database_url)
    _prepare_database(database_url)
    database_provider = DatabaseProvider(settings)
    control_factory = database_provider.control_session_factory()
    queue = JobQueueService()
    worker = OneShotWorker(job_queue=queue, database_provider=database_provider)

    with control_factory.begin() as session:
        busy_workspace = seed_default_workspace(session)
        quiet_workspace = Workspace(
            slug="round-robin-quiet",
            title="Round Robin Quiet",
            name="Round Robin Quiet",
        )
        session.add(quiet_workspace)
        session.flush()
        for index in range(5):
            queue.enqueue(
                session=session,
                workspace_id=busy_workspace.id,
                job_type="probe",
                trigger_type="manual",
                idempotency_key=f"round-robin-busy-{index}",
            )
        quiet_job = queue.enqueue(
            session=session,
            workspace_id=quiet_workspace.id,
            job_type="probe",
            trigger_type="manual",
            idempotency_key="round-robin-quiet-job",
        )
        quiet_job_id = quiet_job.id

    # Force the sweep to start at the busy workspace regardless of how the
    # two workspaces' randomly-generated UUIDs happen to sort -- otherwise
    # this test is only deterministic when busy sorts first, and passes
    # vacuously (without exercising the busy-first case at all) the other
    # half of the time.
    worker._rr_cursor_workspace_id = busy_workspace.id

    claimed_job_ids = [
        worker._claim_via_round_robin(
            control_session_factory=control_factory, job_types=None
        ).id
        for _ in range(2)
    ]

    assert quiet_job_id in claimed_job_ids
    # Round robin took exactly one job from the busy workspace per sweep, not
    # its whole backlog: 5 seeded, at most 1 claimed across these 2 calls.
    with control_factory() as session:
        busy_remaining = len(
            session.scalars(
                select(Job).where(
                    Job.workspace_id == busy_workspace.id,
                    Job.status == "queued",
                )
            ).all()
        )
    assert busy_remaining >= 4


def test_dropped_hint_costs_latency_not_correctness(tmp_path, monkeypatch):
    """Phase 6b (plan §3 D-2): the hint is advisory only. Suppress its write
    entirely -- simulating it being dropped, e.g. a crash between the job's
    own commit and the hint write -- and confirm the job is still found and
    run by the round-robin sweep that _claim_via_hint falls back to when it
    finds nothing. This is the test that matters most: correctness must
    never depend on the hint existing at all.
    """
    monkeypatch.setattr(job_hints, "record_workspace_job_hint", lambda *a, **k: None)

    database_url = f"sqlite+pysqlite:///{tmp_path / 'dropped-hint.db'}"
    settings = Settings(database_url=database_url)
    _prepare_database(database_url)
    database_provider = DatabaseProvider(settings)
    control_factory = database_provider.control_session_factory()
    queue = JobQueueService()

    with control_factory.begin() as session:
        workspace = seed_default_workspace(session)
        registration = LibraryService(job_queue=queue).register_library_item(
            session=session,
            workspace_id=workspace.id,
            canonical_uri="https://example.com/dropped-hint",
            title="Dropped Hint",
            raw_text="dropped hint body",
        )
        job_id = registration.extraction_job.id
        workspace_id = workspace.id

    # The hint write was suppressed above -- no row exists even though a job
    # was enqueued and committed.
    with control_factory() as session:
        assert session.get(WorkspaceJobHint, workspace_id) is None

    worker = OneShotWorker(job_queue=queue, database_provider=database_provider)
    result = worker.dispatch_once_with_session_factory(session_factory=control_factory)

    assert result is not None
    assert result.job_id == job_id
    assert result.status == "completed"


def test_hint_causes_hinted_workspace_claimed_before_an_unhinted_one(tmp_path):
    """A hinted workspace's job is claimed before an unhinted workspace's,
    even when the round-robin roster would otherwise try the unhinted one
    first -- pinning that the hint changes ORDER, not just "eventually"."""
    database_url = f"sqlite+pysqlite:///{tmp_path / 'hint-priority.db'}"
    settings = Settings(database_url=database_url)
    _prepare_database(database_url)
    database_provider = DatabaseProvider(settings)
    control_factory = database_provider.control_session_factory()
    queue = JobQueueService()
    worker = OneShotWorker(job_queue=queue, database_provider=database_provider)

    with control_factory.begin() as session:
        hinted_workspace = Workspace(
            slug="hint-priority-hinted",
            title="Hint Priority Hinted",
            name="Hint Priority Hinted",
        )
        unhinted_workspace = Workspace(
            slug="hint-priority-unhinted",
            title="Hint Priority Unhinted",
            name="Hint Priority Unhinted",
        )
        session.add_all([hinted_workspace, unhinted_workspace])
        session.flush()
        queue.enqueue(
            session=session,
            workspace_id=unhinted_workspace.id,
            job_type="probe",
            trigger_type="manual",
            idempotency_key="hint-priority-unhinted-job",
        )
        hinted_job = queue.enqueue(
            session=session,
            workspace_id=hinted_workspace.id,
            job_type="probe",
            trigger_type="manual",
            idempotency_key="hint-priority-hinted-job",
        )

    # Both enqueues above really did record a hint (the after_commit
    # listener is live in this test, same as production) -- strip
    # unhinted_workspace's back out so exactly one workspace is hinted,
    # isolating what this test is about from hint-writing itself.
    with control_factory.begin() as session:
        stale_row = session.get(WorkspaceJobHint, unhinted_workspace.id)
        assert stale_row is not None
        session.delete(stale_row)

    # Bias the round-robin roster toward the UNHINTED workspace: if the
    # hint mechanism did nothing, plain round robin would claim its job
    # first.
    worker._rr_cursor_workspace_id = unhinted_workspace.id

    # job_type="probe" is deliberately unregistered (same convention as the
    # round-robin test above) -- this test only cares about which job gets
    # claimed, not whether its (nonexistent) business handler succeeds.
    result = worker.dispatch_once_with_session_factory(session_factory=control_factory)

    assert result is not None
    assert result.job_id == hinted_job.id


def test_stale_hint_is_cleared_without_spinning(tmp_path):
    """A hint set with no backing job (already claimed by someone else, or
    -- as constructed directly here -- simply stale) must not wedge or spin:
    _claim_via_hint tries the hinted workspace exactly once, clears the
    hint regardless of the miss, and returns None so the caller's normal
    round-robin fallback still finds real work elsewhere."""
    database_url = f"sqlite+pysqlite:///{tmp_path / 'stale-hint.db'}"
    settings = Settings(database_url=database_url)
    _prepare_database(database_url)
    database_provider = DatabaseProvider(settings)
    control_factory = database_provider.control_session_factory()
    queue = JobQueueService()
    worker = OneShotWorker(job_queue=queue, database_provider=database_provider)

    with control_factory.begin() as session:
        stale_workspace = Workspace(
            slug="stale-hint-workspace",
            title="Stale Hint Workspace",
            name="Stale Hint Workspace",
        )
        other_workspace = Workspace(
            slug="stale-hint-other",
            title="Stale Hint Other",
            name="Stale Hint Other",
        )
        session.add_all([stale_workspace, other_workspace])
        session.flush()
        other_job = queue.enqueue(
            session=session,
            workspace_id=other_workspace.id,
            job_type="probe",
            trigger_type="manual",
            idempotency_key="stale-hint-other-job",
        )
        stale_workspace_id = stale_workspace.id
        other_workspace_id = other_workspace.id

    # other_workspace's enqueue above also recorded a real hint (the
    # after_commit listener is live in this test) -- clear it so it cannot
    # be the one _claim_via_hint's FIFO peek picks below. This test is about
    # a hint with nothing behind it, isolated from any other hint.
    with control_factory.begin() as session:
        other_hint = session.get(WorkspaceJobHint, other_workspace_id)
        assert other_hint is not None
        session.delete(other_hint)

    record_workspace_job_hint(database_provider, stale_workspace_id)

    fast_path_result = worker._claim_via_hint(
        control_session_factory=control_factory, job_types=None
    )
    assert fast_path_result is None

    # Cleared, not left dangling for the next call to trip over again.
    with control_factory() as session:
        assert session.get(WorkspaceJobHint, stale_workspace_id) is None

    # The caller's normal fallback (round robin) still finds the OTHER
    # workspace's real job -- the stale hint didn't wedge anything.
    claimed = worker._claim_via_round_robin(
        control_session_factory=control_factory, job_types=None
    )
    assert claimed is not None
    assert claimed.id == other_job.id
    assert claimed.workspace_id == other_workspace_id


def test_bypass_hint_skips_the_hint_fast_path_entirely(tmp_path, monkeypatch):
    """worker/entry.py's periodic full-sweep timer passes bypass_hint=True
    specifically so round robin's own cursor-rotation fairness can run on a
    fixed cadence independent of hint state -- including the degenerate case
    where hints keep satisfying every call on OTHER workspaces and an
    un-hinted one would otherwise never be reached (plan §3 D-2: "reader
    must be able to see, from the code, that a dropped hint costs latency
    and nothing else").

    A multi-call scenario built around hint hit/clear timing (as in
    test_hint_causes_hinted_workspace_claimed_before_an_unhinted_one) is NOT
    a reliable way to prove this: this test's first draft asserted the same
    two-call outcome as the round-robin starvation test with bypass_hint=True
    and a real hint present on the busy workspace, and it kept passing even
    with the bypass_hint check hard-disabled in runtime.py -- the hint
    path's own FIFO-and-clear dynamics happened to reach the identical
    answer by a different route (busy's one hint gets consumed and cleared
    on the first hint-path hit; the second call's hint peek then finds only
    quiet's own real hint and takes that instead), so the outcome coincided
    regardless of whether bypass_hint did anything. Asserting on that
    outcome would have been exactly this repo's recurring "green test,
    wrong reason" trap. Spying on ``_claim_via_hint`` directly -- was it
    invoked at all -- pins the actual mechanism instead of an emergent
    multi-call outcome.
    """
    database_url = f"sqlite+pysqlite:///{tmp_path / 'bypass-hint.db'}"
    settings = Settings(database_url=database_url)
    _prepare_database(database_url)
    database_provider = DatabaseProvider(settings)
    control_factory = database_provider.control_session_factory()
    queue = JobQueueService()
    worker = OneShotWorker(job_queue=queue, database_provider=database_provider)

    real_claim_via_hint = worker._claim_via_hint
    hint_path_calls: list[None] = []

    def _spy(*args, **kwargs):
        hint_path_calls.append(None)
        return real_claim_via_hint(*args, **kwargs)

    monkeypatch.setattr(worker, "_claim_via_hint", _spy)

    with control_factory.begin() as session:
        workspace = seed_default_workspace(session)
        bypassed_job = queue.enqueue(
            session=session,
            workspace_id=workspace.id,
            job_type="probe",
            trigger_type="manual",
            idempotency_key="bypass-hint-bypassed-job",
        )

    # A real, currently valid hint exists for this workspace.
    with control_factory() as session:
        assert session.get(WorkspaceJobHint, workspace.id) is not None

    bypassed_result = worker.dispatch_once_with_session_factory(
        session_factory=control_factory, bypass_hint=True
    )

    assert bypassed_result is not None
    assert bypassed_result.job_id == bypassed_job.id
    # The core assertion: bypass_hint=True never even calls _claim_via_hint --
    # it does not merely happen to lose to round robin, it is not tried.
    assert hint_path_calls == []

    # Sanity check the spy itself is wired correctly: the SAME worker, with
    # bypass_hint=False (the default) and a fresh hint present, does call
    # the hint path.
    with control_factory.begin() as session:
        queue.enqueue(
            session=session,
            workspace_id=workspace.id,
            job_type="probe",
            trigger_type="manual",
            idempotency_key="bypass-hint-not-bypassed-job",
        )
    worker.dispatch_once_with_session_factory(session_factory=control_factory)
    assert hint_path_calls == [None]


def test_worker_run_cli_reclaims_stale_job_before_dispatch(tmp_path):
    database_url = f"sqlite+pysqlite:///{tmp_path / 'worker-reclaim.db'}"
    settings = Settings(database_url=database_url)
    _prepare_database(database_url)
    session_factory = make_session_factory_from_settings(settings)
    queue = JobQueueService()

    with session_factory.begin() as session:
        workspace = seed_default_workspace(session)
        registration = LibraryService(job_queue=queue).register_library_item(
            session=session,
            workspace_id=workspace.id,
            canonical_uri="https://example.com/worker-reclaim",
            title="Worker Reclaim",
            raw_text="worker reclaim body",
        )
        job_id = registration.extraction_job.id
        claimed = queue.claim_next(
            session=session,
            workspace_id=workspace.id,
            job_types={"extraction"},
        )
        assert claimed.id == job_id
        claimed.started_at = datetime.now(UTC) - timedelta(seconds=1200)
        session.add(claimed)

    result = CliRunner().invoke(
        worker_cli,
        [
            "run",
            "--db",
            database_url,
            "--job-type",
            "extraction",
            "--max-jobs",
            "1",
            "--poll-interval-seconds",
            "0",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout.strip())
    assert payload["job_id"] == str(job_id)
    assert payload["status"] == "completed"

    with session_factory.begin() as session:
        job = session.get(Job, job_id)
        artifact = session.scalar(select(LibraryArtifact))
        index_text = artifact.conversion_detail.index_text
        job_status = job.status
        job_retry_count = job.retry_count

    assert job_status == "completed"
    assert job_retry_count == 1
    assert index_text == "worker reclaim body"


def test_worker_run_cli_survives_transient_dispatch_error(tmp_path, monkeypatch):
    """The long-running `run` loop must not die on a transient infrastructure
    error (e.g. an OperationalError from a dropped DB connection during the
    claim transaction): it logs, emits an error status line, backs off, and
    keeps polling until the next dispatch succeeds."""
    database_url = f"sqlite+pysqlite:///{tmp_path / 'worker-resilient.db'}"
    settings = Settings(database_url=database_url)
    _prepare_database(database_url)
    session_factory = make_session_factory_from_settings(settings)

    with session_factory.begin() as session:
        workspace = seed_default_workspace(session)
        registration = LibraryService().register_library_item(
            session=session,
            workspace_id=workspace.id,
            canonical_uri="https://example.com/worker-resilient",
            title="Worker Resilient",
            raw_text="resilient body",
        )
        job_id = registration.extraction_job.id

    real_dispatch = OneShotWorker.dispatch_once_with_session_factory
    dispatch_calls = {"count": 0}

    def _flaky_dispatch(self, **kwargs):
        dispatch_calls["count"] += 1
        if dispatch_calls["count"] == 1:
            raise OperationalError("SELECT 1", {}, RuntimeError("db restart"))
        return real_dispatch(self, **kwargs)

    monkeypatch.setattr(
        OneShotWorker, "dispatch_once_with_session_factory", _flaky_dispatch
    )
    sleeps: list[float] = []
    monkeypatch.setattr(worker_entry.time, "sleep", sleeps.append)

    result = CliRunner().invoke(
        worker_cli,
        [
            "run",
            "--db",
            database_url,
            "--job-type",
            "extraction",
            "--max-jobs",
            "1",
            "--poll-interval-seconds",
            "0",
        ],
    )

    assert result.exit_code == 0
    payloads = [
        json.loads(line) for line in result.stdout.splitlines() if line.startswith("{")
    ]
    assert payloads[0]["status"] == "error"
    assert "db restart" in payloads[0]["error"]
    assert payloads[-1]["status"] == "completed"
    assert dispatch_calls["count"] == 2
    # The error path backs off before retrying.
    assert sleeps and sleeps[0] == 1.0

    with session_factory.begin() as session:
        job = session.get(Job, job_id)
        artifact = session.scalar(select(LibraryArtifact))
        index_text = artifact.conversion_detail.index_text
        job_status = job.status

    assert job_status == "completed"
    assert index_text == "resilient body"


def test_two_phase_dispatch_returns_none_when_no_job():
    """Sanity: when the queue is empty, the two-phase dispatcher returns
    None and does not attempt phases 1 or 2."""
    session_factory = make_session_factory()
    queue = JobQueueService()
    worker = OneShotWorker(job_queue=queue)

    with session_factory.begin() as session:
        seed_default_workspace(session)

    result = worker.dispatch_once_with_session_factory(
        session_factory=session_factory,
    )
    assert result is None


def test_repeated_email_sweeps_commit_the_private_audit_once(monkeypatch):
    """INVERTED by MAIL-02: this used to dispatch the email_delivery Job three
    times and assert the JOB row carried the retry budget (queued, queued,
    failed). That Job is gone -- the budget lives on the outbox row now
    (EmailDeliveryService._record_attempt_failure) -- so the same property is
    proven by sweeping three times instead: bounded retries with growing
    backoff, then one terminal marker and exactly one private audit event."""

    class FailingProvider:
        def send(self, message: OutboundMail) -> None:
            raise MailProviderError("provider echoed recipient@example.com")

    # Every deadline in this test is the delivery service's own: the sweep's
    # due predicate and the failure backoff read ``mail_delivery.utc_now``, the
    # reset token's lifetime reads the recovery service's ``now``. The host
    # clock steps back (NTP, VM resume) and jumps forward (host sleep), so a
    # test that backdates a row against it, or measures a backoff around a
    # sweep, is right only while no step lands inside it. Both services read
    # this one clock instead, and time passes by assignment.
    clock = [datetime.now(UTC)]
    monkeypatch.setattr("backend.services.mail_delivery.utc_now", lambda: clock[0])

    session_factory = make_session_factory()
    settings = Settings(
        public_base_url="https://wiki.example.com",
        session_secret="worker-email-test-session-secret",
        mail_provider="smtp",
        mail_from_address="wiki@example.com",
        smtp_host="smtp.example.com",
    )
    delivery = EmailDeliveryService(settings=settings, provider=FailingProvider())
    with session_factory.begin() as session:
        seed_default_workspace(session)
        user = User(
            email="worker-email@example.com",
            password_hash=CredentialHasher().hash_password("original-password"),
            display_name="Worker email",
            status="active",
            email_verified_at=clock[0],
        )
        session.add(user)
        session.flush()
        user_id = user.id
    issued = PasswordRecoveryService(
        session_factory=session_factory,
        credential_hasher=CredentialHasher(),
        public_base_url=settings.public_base_url,
        mail_delivery_service=delivery,
        # ``now`` is bound when the class is defined, so patching the module's
        # ``utc_now`` would not reach it.
        now=lambda: clock[0],
    ).issue_password_reset(
        email="worker-email@example.com",
        delivery="email",
        requested_via="public",
    )

    attempt_counts = []
    retry_delays = []
    for _attempt in range(DEFAULT_JOB_MAX_ATTEMPTS):
        assert delivery.sweep_due(session_factory=session_factory) == 0
        with session_factory.begin() as session:
            outbox = session.get(EmailOutbox, issued.outbox_id)
            assert outbox is not None and outbox.status == "failed"
            attempt_counts.append(outbox.attempt_count)
            retry_at = outbox.next_attempt_at
            if retry_at is not None:
                retry_delays.append((retry_at - clock[0]).total_seconds())

        # Before its deadline, or once the retry budget is spent, another
        # sweep must not consume another attempt.
        assert delivery.sweep_due(session_factory=session_factory) == 0
        with session_factory() as session:
            outbox = session.get(EmailOutbox, issued.outbox_id)
            assert outbox is not None
            assert outbox.attempt_count == attempt_counts[-1]

        if retry_at is not None:
            # Elapse the backoff so the next sweep sees the row as due. Only a
            # deadline can make a failed row due: NULL is the terminal marker
            # the due predicate refuses, which is what bounds the retries at
            # all.
            clock[0] = retry_at

    assert attempt_counts == [1, 2, 3]
    assert retry_delays == [30.0, 60.0]
    with session_factory() as session:
        outbox = session.get(EmailOutbox, issued.outbox_id)
        events = list(
            session.scalars(
                select(AuditEvent).where(
                    AuditEvent.action == "auth.email.delivery.failed"
                )
            ).all()
        )
        assert outbox is not None and outbox.status == "failed"
        # The terminal marker now carries the provider's own stable error
        # category; the deleted on_failure handler flattened every worker
        # failure to "mail_delivery_failed" regardless of cause.
        assert outbox.last_error == "mail_provider_failed"
        assert outbox.next_attempt_at is None
        assert outbox.encrypted_token is None
        assert "worker-email@example.com" not in (outbox.last_error or "")
        assert len(events) == 1
        assert events[0].target_id == user_id
        # This exact-dict comparison subsumes the recipient-address check
        # that used to sit beside it ("worker-email@example.com" not in
        # str(payload)): a payload that leaked the address could not equal
        # this dict. Stated rather than left implicit, because dropping a
        # privacy assertion silently is how one gets lost for real.
        assert events[0].payload == {
            "message_kind": "password_reset",
            "delivery_path": "worker",
            "error_code": "mail_provider_failed",
        }


def test_worker_reclaim_requeues_lint_run_with_retryable_job():
    session_factory = make_session_factory()
    queue = JobQueueService()
    worker = OneShotWorker(job_queue=queue)

    with session_factory.begin() as session:
        workspace = seed_default_workspace(session)
        run = LintService(job_queue=queue).enqueue_run(
            session,
            workspace.id,
            LintRunCreateRequest(),
            actor_kind="human",
            actor_id="test-user",
        )
        job = queue.claim_next(
            session=session,
            workspace_id=workspace.id,
            job_types={"lint_tick"},
        )
        assert job is not None
        run.status = "running"
        run.started_at = datetime.now(UTC) - timedelta(seconds=1200)
        job.started_at = datetime.now(UTC) - timedelta(seconds=1200)
        run_id = run.id
        job_id = job.id

    assert (
        worker.reclaim_stale_jobs_with_session_factory(
            session_factory=session_factory,
            lease_timeout_seconds=600,
        )
        == 1
    )

    with session_factory() as session:
        stored_run = session.get(LintRun, run_id)
        stored_job = session.get(Job, job_id)

    assert stored_job.status == "queued"
    assert stored_job.retry_count == 1
    assert stored_run.status == "queued"
    assert stored_run.started_at is None
    assert stored_run.completed_at is None


def test_worker_requeue_ignores_foreign_lint_run_without_mutation():
    session_factory = make_session_factory()
    queue = JobQueueService()
    worker = OneShotWorker(job_queue=queue)
    started_at = datetime(2026, 8, 31, 1, 2, tzinfo=UTC)

    with session_factory.begin() as session:
        workspace = seed_default_workspace(session)
        foreign_workspace = Workspace(
            slug="worker-foreign-lint-state",
            title="Worker foreign lint state",
            name="Worker foreign lint state",
        )
        session.add(foreign_workspace)
        session.flush()
        foreign_run = LintRun(
            workspace_id=foreign_workspace.id,
            lint_scope={"kind": "workspace"},
            trigger_type="manual",
            status="running",
            started_at=started_at,
        )
        session.add(foreign_run)
        session.flush()
        forged_job = queue.enqueue(
            session=session,
            workspace_id=workspace.id,
            job_type="lint_tick",
            trigger_type="scheduled",
            idempotency_key="worker-foreign-lint-state",
            target_ref={"lint_run_id": str(foreign_run.id)},
        )
        foreign_run_id = foreign_run.id

        worker._reset_requeued_domain_state(session, forged_job)

    with session_factory() as session:
        stored_foreign_run = session.get(LintRun, foreign_run_id)

    assert stored_foreign_run is not None
    assert stored_foreign_run.status == "running"
    assert stored_foreign_run.started_at == started_at
    assert stored_foreign_run.completed_at is None


def test_worker_terminal_reclaim_marks_lint_run_failed():
    session_factory = make_session_factory()
    queue = JobQueueService()
    worker = OneShotWorker(job_queue=queue)

    with session_factory.begin() as session:
        workspace = seed_default_workspace(session)
        run = LintService(job_queue=queue).enqueue_run(
            session,
            workspace.id,
            LintRunCreateRequest(),
            actor_kind="human",
            actor_id="test-user",
        )
        job = queue.claim_next(
            session=session,
            workspace_id=workspace.id,
            job_types={"lint_tick"},
        )
        assert job is not None
        run.status = "running"
        run.started_at = datetime.now(UTC) - timedelta(seconds=1200)
        job.started_at = datetime.now(UTC) - timedelta(seconds=1200)
        job.retry_count = DEFAULT_JOB_MAX_ATTEMPTS - 1
        run_id = run.id
        job_id = job.id

    assert (
        worker.reclaim_stale_jobs_with_session_factory(
            session_factory=session_factory,
            lease_timeout_seconds=600,
        )
        == 1
    )

    with session_factory() as session:
        stored_run = session.get(LintRun, run_id)
        stored_job = session.get(Job, job_id)

    assert stored_job.status == "failed"
    assert stored_job.retry_count == DEFAULT_JOB_MAX_ATTEMPTS
    assert stored_run.status == "failed"
    assert stored_run.completed_at is not None


def test_worker_run_once_sweeps_the_email_outbox(tmp_path, monkeypatch):
    """MAIL-02: `worker run-once` never swept the outbox -- only the
    long-running `run` loop did. With the email_delivery Job path deleted the
    sweep is the ONLY delivery route, so a one-shot worker that skips it can
    never deliver a password-reset mail at all."""
    database_url = f"sqlite+pysqlite:///{tmp_path / 'worker-once-mail.db'}"
    monkeypatch.setenv("DTXWIKI_PUBLIC_BASE_URL", "https://wiki.example.com")
    monkeypatch.setenv("DTXWIKI_MAIL_PROVIDER", "smtp")
    monkeypatch.setenv("DTXWIKI_MAIL_FROM_ADDRESS", "wiki@example.com")
    monkeypatch.setenv("DTXWIKI_SMTP_HOST", "smtp.example.com")
    settings = Settings(database_url=database_url)
    _prepare_database(database_url)
    session_factory = make_session_factory_from_settings(settings)
    with session_factory.begin() as session:
        user = User(
            email="run-once-mail@example.com",
            password_hash=CredentialHasher().hash_password("original-password"),
            display_name="Run once mail",
            status="active",
            email_verified_at=datetime.now(UTC),
        )
        session.add(user)
        session.flush()
        outbox = EmailOutbox(
            recipient_user_id=user.id,
            recipient_email=user.email,
            message_kind="password_changed",
            status="queued",
        )
        session.add(outbox)
        session.flush()
        outbox_id = outbox.id

    sent: list[OutboundMail] = []
    monkeypatch.setattr(
        SMTPMailProvider, "send", lambda _self, message: sent.append(message)
    )

    result = CliRunner().invoke(
        worker_cli,
        ["run-once", "--db", database_url, "--skip-bootstrap"],
    )

    assert result.exit_code == 0, result.output
    assert len(sent) == 1
    with session_factory() as session:
        delivered = session.get(EmailOutbox, outbox_id)
        assert delivered is not None and delivered.status == "sent"


def test_worker_run_loop_sweeps_the_email_outbox_each_iteration(tmp_path, monkeypatch):
    """N6: the long-running ``worker run`` loop IS the production delivery
    route, and nothing covered it -- deleting its whole email-sweep timer block
    left the suite green because the only coverage was for ``run-once``.

    The row is queued only AFTER the loop has started (from the first idle
    sleep), so the one pre-loop sweep cannot see it: delivery here can only
    come from the timer block inside the loop.
    """
    database_url = f"sqlite+pysqlite:///{tmp_path / 'worker-run-loop-mail.db'}"
    monkeypatch.setenv("DTXWIKI_PUBLIC_BASE_URL", "https://wiki.example.com")
    monkeypatch.setenv("DTXWIKI_MAIL_PROVIDER", "smtp")
    monkeypatch.setenv("DTXWIKI_MAIL_FROM_ADDRESS", "wiki@example.com")
    monkeypatch.setenv("DTXWIKI_SMTP_HOST", "smtp.example.com")
    settings = Settings(database_url=database_url)
    _prepare_database(database_url)
    session_factory = make_session_factory_from_settings(settings)
    with session_factory.begin() as session:
        user = User(
            email="run-loop-mail@example.com",
            password_hash=CredentialHasher().hash_password("original-password"),
            display_name="Run loop mail",
            status="active",
            email_verified_at=datetime.now(UTC),
        )
        session.add(user)
        session.flush()
        user_id = user.id

    sent: list[OutboundMail] = []
    monkeypatch.setattr(
        SMTPMailProvider, "send", lambda _self, message: sent.append(message)
    )
    # The loop's own minimum-interval timer is 1 s of wall clock; drop it so
    # the test does not have to sleep through it. The block under test is the
    # timer's body, not its cadence.
    monkeypatch.setattr(worker_entry, "_EMAIL_SWEEP_MIN_INTERVAL_SECONDS", 0.0)

    outbox_ids: list[UUID] = []

    def _queue_on_first_idle(_seconds: float) -> None:
        if outbox_ids:
            return
        with session_factory.begin() as session:
            outbox = EmailOutbox(
                recipient_user_id=user_id,
                recipient_email="run-loop-mail@example.com",
                message_kind="password_changed",
                status="queued",
            )
            session.add(outbox)
            session.flush()
            outbox_ids.append(outbox.id)

    monkeypatch.setattr(worker_entry.time, "sleep", _queue_on_first_idle)

    result = CliRunner().invoke(
        worker_cli,
        [
            "run",
            "--db",
            database_url,
            "--skip-bootstrap",
            "--idle-exit-after",
            "3",
            "--poll-interval-seconds",
            "0",
        ],
    )

    assert result.exit_code == 0, result.output
    assert outbox_ids, "the loop never went idle, so nothing was ever queued"
    assert len(sent) == 1, (
        "the run loop must sweep the outbox on its own cadence; a row queued "
        "after startup was never delivered"
    )
    assert sent[0].recipient == "run-loop-mail@example.com"
    with session_factory() as session:
        delivered = session.get(EmailOutbox, outbox_ids[0])
        assert delivered is not None and delivered.status == "sent"

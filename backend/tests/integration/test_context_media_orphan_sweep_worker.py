"""The orphan-sweep job is seeded per workspace, dispatched by its registered
handler, and reports its retention setting correctly.
"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from backend.app.settings import Settings
from backend.context_media.adapters.sqlalchemy import SqlAlchemyContextMediaRepository
from backend.context_media.application import RegisterMediaAsset as _RegisterMediaAsset
from backend.context_media.domain import ActorIdentity, RegisterContextMediaAsset
from backend.media.domain import BlobEpoch, BlobReference
from backend.models.base import Base
from backend.models.context import ContextPage, ContextPageVersion
from backend.models.context_media import ContextMediaAsset
from backend.models.job import Job
from backend.models.media import (
    BlobIncarnationRow,
    StoredBlobInspectionRow,
    StoredBlobRow,
    WorkspaceMediaWriteIntent,
)
from backend.scripts.seed_dev import seed_default_workspace
from backend.services.context_media_orphan_sweep import (
    ContextMediaOrphanSweepService,
    OrphanSweepResult,
)
from backend.services.job_queue import JobQueueService
from backend.worker.registry import registered_handlers
from backend.worker.runtime import OneShotWorker
from backend.worker.tick import seed_due_jobs


def make_session_factory():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def test_context_media_orphan_sweep_is_registered():
    assert "context_media_orphan_sweep" in registered_handlers()


def test_tick_seeds_orphan_sweep_only_when_retention_is_enabled():
    session_factory = make_session_factory()
    now = datetime(2026, 8, 29, 3, 0, tzinfo=UTC)
    disabled_settings = Settings(
        lint_tick_interval_hours=0,
        trail_tick_interval_hours=0,
        librarian_tick_interval_days=0,
        context_media_orphan_retention_hours=0,
    )
    enabled_settings = Settings(
        lint_tick_interval_hours=0,
        trail_tick_interval_hours=0,
        librarian_tick_interval_days=0,
        context_media_orphan_retention_hours=168,
    )
    with session_factory.begin() as session:
        seed_default_workspace(session)
        disabled = seed_due_jobs(session, disabled_settings, now=now)
        assert "context_media_orphan_sweep" not in {
            job.job_type for job in disabled
        }

    with session_factory.begin() as session:
        enabled = seed_due_jobs(session, enabled_settings, now=now)
        assert "context_media_orphan_sweep" in {job.job_type for job in enabled}


def test_orphan_sweep_is_seeded_on_the_interval_schedule_not_per_minute():
    """Epic-2 D-7 scheduling correction: day-scale retention does not belong
    in the per-minute bucket alongside bundle_expiration_sweep/
    media_install_reconcile (worker/tick.py) -- per-minute-per-workspace
    seeding is exactly what Phase 6's job-queue fan-out has to scale, and
    workspace count tracks subscriber count. Pinned via the idempotency
    key's shape, which only the interval schedule produces: a minute-bucket
    job's key is f"{workspace_id}:{minute_bucket}", with no interval or
    bucket count embedded, so this assertion would have failed under the
    old placement rather than passing vacuously."""
    session_factory = make_session_factory()
    now = datetime(2026, 8, 29, 3, 0, tzinfo=UTC)
    settings = Settings(
        lint_tick_interval_hours=0,
        trail_tick_interval_hours=0,
        librarian_tick_interval_days=0,
        context_media_orphan_retention_hours=168,
    )
    with session_factory.begin() as session:
        seed_default_workspace(session)
        seeded = seed_due_jobs(session, settings, now=now)

    orphan_jobs = [
        job for job in seeded if job.job_type == "context_media_orphan_sweep"
    ]
    assert len(orphan_jobs) == 1
    # 168h retention is bounded down to a 24h check interval (never
    # coarser than daily); the bucket count is baked into the idempotency
    # key the same way lint_tick's interval-bucketed key is.
    bucket = int(now.timestamp()) // (24 * 3600)
    assert orphan_jobs[0].idempotency_key == f"context_media_orphan_sweep:24h:{bucket}"


def test_orphan_sweep_interval_never_exceeds_a_short_retention_window():
    """A short retention setting is not silently rounded up to a day of
    slop -- the check interval never exceeds the window itself."""
    session_factory = make_session_factory()
    now = datetime(2026, 8, 29, 3, 0, tzinfo=UTC)
    settings = Settings(
        lint_tick_interval_hours=0,
        trail_tick_interval_hours=0,
        librarian_tick_interval_days=0,
        context_media_orphan_retention_hours=2,
    )
    with session_factory.begin() as session:
        seed_default_workspace(session)
        seeded = seed_due_jobs(session, settings, now=now)

    orphan_jobs = [
        job for job in seeded if job.job_type == "context_media_orphan_sweep"
    ]
    assert len(orphan_jobs) == 1
    bucket = int(now.timestamp()) // (2 * 3600)
    assert orphan_jobs[0].idempotency_key == f"context_media_orphan_sweep:2h:{bucket}"


def test_worker_dispatches_registered_orphan_sweep_service():
    """Wiring test with a fake service (mirrors
    test_worker_dispatches_registered_media_install_reconciler): the
    handler must pass job.workspace_id, settings.retention_hours, and the
    job's scheduled_for target through unchanged."""

    session_factory = make_session_factory()
    queue = JobQueueService()
    scheduled_for = datetime(2026, 8, 29, 1, 2, tzinfo=UTC)

    class _FakeSweepService:
        called = None

        def sweep(self, session, *, workspace_id, retention_hours, now):
            assert session is not None
            self.called = (workspace_id, retention_hours, now)
            return OrphanSweepResult(
                inspected_count=5,
                reclaimed_count=2,
                drained_count=1,
                run_at=now,
            )

    fake_service = _FakeSweepService()
    with session_factory.begin() as session:
        workspace = seed_default_workspace(session)
        queue.enqueue(
            session=session,
            workspace_id=workspace.id,
            job_type="context_media_orphan_sweep",
            trigger_type="scheduled",
            idempotency_key="orphan-sweep:test",
            target_ref={"scheduled_for": scheduled_for.isoformat()},
        )
        workspace_id = workspace.id

    worker = OneShotWorker(
        job_queue=queue,
        context_media_orphan_sweep_service=fake_service,
        settings=Settings(context_media_orphan_retention_hours=72),
    )
    with session_factory.begin() as session:
        result = worker.dispatch_once(
            session,
            workspace_id=workspace_id,
            job_types={"context_media_orphan_sweep"},
        )

    assert result is not None
    assert result.status == "completed"
    assert result.detail == {
        "inspected_count": 5,
        "reclaimed_count": 2,
        "drained_count": 1,
    }
    assert fake_service.called == (workspace_id, 72, scheduled_for)


def test_worker_end_to_end_reclaims_a_real_orphan():
    """No fake: a real closed-editing page-bound orphan, a real
    ContextMediaOrphanSweepService, a real job dispatch."""

    session_factory = make_session_factory()
    queue = JobQueueService()
    scheduled_for = datetime(2026, 8, 29, 1, 2, tzinfo=UTC)

    with session_factory.begin() as session:
        workspace = seed_default_workspace(session)
        workspace_id = workspace.id
        if session.get(WorkspaceMediaWriteIntent, workspace_id) is None:
            session.add(
                WorkspaceMediaWriteIntent(
                    workspace_id=workspace_id,
                    state="open",
                    delete_generation=0,
                    updated_at=scheduled_for,
                )
            )
            session.flush()
        blob_id = uuid4()
        session.add(
            StoredBlobRow(
                workspace_id=workspace_id, id=blob_id, sha256="b" * 64, next_epoch=2
            )
        )
        session.flush()
        session.add(
            BlobIncarnationRow(
                workspace_id=workspace_id,
                blob_id=blob_id,
                blob_epoch=1,
                byte_size=68,
                storage_key=f"w/{workspace_id.hex}/a/{blob_id.hex}",
                state="available",
                publish_generation=1,
                security_generation=0,
                gc_generation=0,
                available_at=scheduled_for,
            )
        )
        session.flush()
        inspection_id = uuid4()
        session.add(
            StoredBlobInspectionRow(
                workspace_id=workspace_id,
                id=inspection_id,
                blob_id=blob_id,
                blob_epoch=1,
                registry_version="registry-v1",
                detector_id="png-magic",
                detector_version="1",
                format_id="png",
                canonical_media_type="image/png",
                validation_schema_version="png-v1",
                metadata_json={},
                scanner_verdict="not_configured",
                scanner_provider=None,
                scanner_version=None,
                ordinary_available=True,
                created_at=scheduled_for,
            )
        )
        session.flush()

        repository = SqlAlchemyContextMediaRepository(session)
        actor = ActorIdentity("human", str(uuid4()))
        page = ContextPage(workspace_id=workspace_id, slug="worker-orphan",
            title="Worker orphan", page_type="overview")
        session.add(page)
        session.flush()
        version = ContextPageVersion(workspace_id=workspace_id, page_id=page.id,
            version_no=1, body_md="", content_hash="0" * 64, actor_type="human",
            created_by_actor_kind=actor.kind, created_by_actor_id=actor.id,
            version_status="editing")
        session.add(version)
        session.flush()
        asset = _RegisterMediaAsset().execute(
            repository,
            RegisterContextMediaAsset(
                workspace_id=workspace_id,
                asset_id=uuid4(),
                page_id=page.id,
                created_for_version_id=version.id,
                blob=BlobReference(workspace_id, blob_id, BlobEpoch(1)),
                inspection_id=inspection_id,
                original_filename="pixel.png",
                canonical_media_type="image/png",
                logical_bytes=68,
                policy_revision="context-mvp-v1",
                actor=actor,
            ),
            now=scheduled_for - timedelta(days=2),
        )
        asset_id = asset.asset_id
        version.version_status = "draft"
        row = session.get(ContextMediaAsset, (workspace_id, asset_id))
        row.created_at = scheduled_for - timedelta(days=2)
        session.flush()

        queue.enqueue(
            session=session,
            workspace_id=workspace_id,
            job_type="context_media_orphan_sweep",
            trigger_type="scheduled",
            idempotency_key="orphan-sweep:e2e",
            target_ref={"scheduled_for": scheduled_for.isoformat()},
        )

    worker = OneShotWorker(
        job_queue=queue,
        context_media_orphan_sweep_service=ContextMediaOrphanSweepService(
            SqlAlchemyContextMediaRepository
        ),
        settings=Settings(context_media_orphan_retention_hours=1),
    )
    with session_factory.begin() as session:
        result = worker.dispatch_once(
            session,
            workspace_id=workspace_id,
            job_types={"context_media_orphan_sweep"},
        )

    assert result is not None
    assert result.status == "completed"
    assert result.detail["reclaimed_count"] == 1
    assert result.detail["drained_count"] == 1

    with session_factory.begin() as session:
        # Drained in the same tick, not merely tombstoned.
        row = session.get(
            ContextMediaAsset, {"workspace_id": workspace_id, "id": asset_id}
        )
        assert row is None
        job = session.scalar(
            select(Job).where(
                Job.workspace_id == workspace_id,
                Job.job_type == "context_media_orphan_sweep",
            )
        )
        assert job is not None and job.status == "completed"

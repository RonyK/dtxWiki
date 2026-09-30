from datetime import UTC, datetime, timedelta

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from backend.models.audit import AuditEvent
from backend.models.base import Base
from backend.models.bundle import AgentInputBundle
from backend.models.job import Job
from backend.models.workspace import MCPClient
from backend.scripts.seed_dev import seed_default_workspace
from backend.services.bundle_expiration import BundleExpirationService
from backend.services.job_queue import JobQueueService


def make_session_factory():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def _test_mcp_client(session, workspace_id):
    client = MCPClient(
        workspace_id=workspace_id,
        name="bundle-expiration-test-client",
        client_type="api_token",
        scopes=["mcp:read"],
        status="active",
    )
    session.add(client)
    session.flush()
    return client


def test_bundle_expiration_sweep_expires_due_bundles_and_completes_job():
    session_factory = make_session_factory()
    queue = JobQueueService()
    expiration_service = BundleExpirationService(job_queue=queue)
    now = datetime(2026, 5, 9, 16, 40, tzinfo=UTC)

    with session_factory.begin() as session:
        workspace = seed_default_workspace(session)
        client = _test_mcp_client(session, workspace.id)
        expired = _bundle(
            workspace_id=workspace.id,
            client_id=client.id,
            valid_until=now - timedelta(seconds=1),
        )
        active = _bundle(
            workspace_id=workspace.id,
            client_id=client.id,
            valid_until=now + timedelta(seconds=60),
        )
        session.add_all([expired, active])
        job = expiration_service.enqueue_sweep(
            session=session,
            workspace_id=workspace.id,
            at=now,
        )
        claimed = queue.claim_next(session=session, workspace_id=workspace.id)
        result = expiration_service.sweep_expired(
            session=session,
            workspace_id=workspace.id,
            now=now,
            job=claimed,
        )

    with session_factory.begin() as session:
        expired = session.get(AgentInputBundle, expired.id)
        active = session.get(AgentInputBundle, active.id)
        job = session.get(Job, job.id)
        audits = session.scalars(
            select(AuditEvent).where(AuditEvent.action == "bundle.expire")
        ).all()

    assert result.expired_count == 1
    assert result.inspected_count == 1
    assert expired.status == "expired"
    assert expired.expiration_reason == "ttl_elapsed"
    # expired_at round-trips as offset-aware UTC now that the column uses the
    # UTCDateTime decorator (previously SQLite handed back a naive datetime, the
    # very bug this guards against), so compare against the aware `now` directly.
    assert expired.expired_at == now
    assert active.status == "generated"
    assert job.status == "completed"
    assert len(audits) == 1
    assert audits[0].target_id == expired.id


def test_bundle_expiration_sweep_is_idempotent():
    session_factory = make_session_factory()
    expiration_service = BundleExpirationService()
    now = datetime(2026, 5, 9, 16, 41, tzinfo=UTC)

    with session_factory.begin() as session:
        workspace = seed_default_workspace(session)
        client = _test_mcp_client(session, workspace.id)
        session.add(
            _bundle(
                workspace_id=workspace.id,
                client_id=client.id,
                valid_until=now - timedelta(seconds=1),
            )
        )
        first = expiration_service.sweep_expired(
            session=session,
            workspace_id=workspace.id,
            now=now,
        )
        second = expiration_service.sweep_expired(
            session=session,
            workspace_id=workspace.id,
            now=now,
        )

    with session_factory.begin() as session:
        audits = session.scalars(
            select(AuditEvent).where(AuditEvent.action == "bundle.expire")
        ).all()

    assert first.expired_count == 1
    assert second.expired_count == 0
    assert second.inspected_count == 0
    assert len(audits) == 1


def test_bundle_expiration_enqueue_is_idempotent_per_minute_bucket():
    session_factory = make_session_factory()
    expiration_service = BundleExpirationService()
    first_at = datetime(2026, 5, 9, 16, 42, 10, tzinfo=UTC)
    same_bucket_at = datetime(2026, 5, 9, 16, 42, 59, tzinfo=UTC)
    next_bucket_at = datetime(2026, 5, 9, 16, 43, tzinfo=UTC)

    with session_factory.begin() as session:
        workspace = seed_default_workspace(session)
        first = expiration_service.enqueue_sweep(
            session=session,
            workspace_id=workspace.id,
            at=first_at,
        )
        same_bucket = expiration_service.enqueue_sweep(
            session=session,
            workspace_id=workspace.id,
            at=same_bucket_at,
        )
        next_bucket = expiration_service.enqueue_sweep(
            session=session,
            workspace_id=workspace.id,
            at=next_bucket_at,
        )

    with session_factory.begin() as session:
        jobs = session.scalars(select(Job).order_by(Job.created_at, Job.id)).all()

    assert first.id == same_bucket.id
    assert first.id != next_bucket.id
    assert len(jobs) == 2
    assert {job.job_type for job in jobs} == {"bundle_expiration_sweep"}


def _bundle(
    workspace_id,
    client_id,
    valid_until,
) -> AgentInputBundle:
    return AgentInputBundle(
        workspace_id=workspace_id,
        client_id=client_id,
        task_intent="answer",
        token_budget=2048,
        ttl_seconds=3600,
        valid_until=valid_until,
    )

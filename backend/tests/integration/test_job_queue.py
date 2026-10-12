import random
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from backend.app.db import make_engine
from backend.models.base import Base, utc_now
from backend.models.job import Job
from backend.models.workspace import Workspace
from backend.scripts.seed_dev import seed_default_workspace
from backend.services.job_queue import (
    DEFAULT_JOB_MAX_ATTEMPTS,
    JOB_TYPES_WITH_DOMAIN_FAILURE_STATE,
    MAX_RETRY_BACKOFF_SECONDS,
    JobQueueService,
    JobRetryUnsupportedTypeError,
    calculate_retry_delay,
    context_page_embed_idempotency_key,
)


def make_session_factory():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture
def queue_clock(monkeypatch):
    """The queue's "now", moved only when a test moves it.

    ``JobQueueService`` reads the wall clock through ``job_queue.utc_now`` when
    it claims, reclaims, fails and completes a job, and the host clock is not
    monotonic: an NTP or VM-resume step sets it back, and a host sleep jumps it
    forward. A test that backdates a deadline against ``utc_now()``, or
    measures a retry window around ``fail()``, is therefore right only while
    no step lands inside it. Here time passes by assignment and the queue's
    decisions follow it exactly. Model defaults such as ``created_at`` keep the
    host clock, so a test that orders jobs by it assigns it from this clock.
    """

    # An anchor only: nothing reads the host clock through it again.
    clock = [utc_now()]
    monkeypatch.setattr("backend.services.job_queue.utc_now", lambda: clock[0])
    return clock


def test_job_queue_enqueue_is_idempotent_per_workspace_type_key():
    session_factory = make_session_factory()
    queue = JobQueueService()

    with session_factory.begin() as session:
        workspace = seed_default_workspace(session)
        first = queue.enqueue(
            session=session,
            workspace_id=workspace.id,
            job_type="library_discovery",
            trigger_type="manual",
            idempotency_key="workspace-policy-run",
        )
        second = queue.enqueue(
            session=session,
            workspace_id=workspace.id,
            job_type="library_discovery",
            trigger_type="manual",
            idempotency_key="workspace-policy-run",
        )

    with session_factory.begin() as session:
        jobs = session.scalars(select(Job)).all()

    assert first.id == second.id
    assert len(jobs) == 1
    assert jobs[0].status == "queued"


def test_job_hint_waits_for_outer_commit_across_savepoint(monkeypatch):
    session_factory = make_session_factory()
    queue = JobQueueService()
    recorded: list = []
    monkeypatch.setattr(
        "backend.services.job_hints.record_workspace_job_hint",
        lambda _provider, workspace_id: recorded.append(workspace_id),
    )

    with session_factory() as session:
        session.info["database_provider"] = object()
        workspace = seed_default_workspace(session)
        with session.begin_nested():
            queue.enqueue(
                session=session,
                workspace_id=workspace.id,
                job_type="library_discovery",
                trigger_type="manual",
                idempotency_key="nested-commit-hint",
            )
        assert recorded == []
        session.commit()

    assert recorded == [workspace.id]


def test_job_hint_from_rolled_back_savepoint_is_not_published(monkeypatch):
    session_factory = make_session_factory()
    queue = JobQueueService()
    recorded: list = []
    monkeypatch.setattr(
        "backend.services.job_hints.record_workspace_job_hint",
        lambda _provider, workspace_id: recorded.append(workspace_id),
    )

    with session_factory() as session:
        workspace = seed_default_workspace(session)
        session.commit()
        session.info["database_provider"] = object()
        nested = session.begin_nested()
        queue.enqueue(
            session=session,
            workspace_id=workspace.id,
            job_type="library_discovery",
            trigger_type="manual",
            idempotency_key="nested-rollback-hint",
        )
        nested.rollback()
        session.commit()

    assert recorded == []


def test_context_embed_key_changes_when_content_reverts_to_prior_hash():
    page_id = uuid4()
    first_version_id = uuid4()
    reverted_version_id = uuid4()

    first = context_page_embed_idempotency_key(
        page_id, "published", first_version_id, "same-content-hash"
    )
    reverted = context_page_embed_idempotency_key(
        page_id, "published", reverted_version_id, "same-content-hash"
    )

    assert first != reverted
    assert str(first_version_id) in first
    assert str(reverted_version_id) in reverted


def test_fail_sets_exponential_backoff_and_claim_skips_until_due(queue_clock):
    session_factory = make_session_factory()
    queue = JobQueueService()

    with session_factory.begin() as session:
        workspace = seed_default_workspace(session)
        job = queue.enqueue(
            session=session,
            workspace_id=workspace.id,
            job_type="embed",
            trigger_type="content_version",
            idempotency_key="doc-1",
        )
        claimed = queue.claim_next(session, job_types={"embed"})
        assert claimed is not None and claimed.id == job.id

        queue.fail(session, claimed, "rate limited")
        # Requeued with a future next_attempt_at instead of immediately.
        assert claimed.status == "queued"
        assert claimed.next_attempt_at is not None
        assert claimed.next_attempt_at > queue_clock[0]
        retry_at = claimed.next_attempt_at
        # While the backoff window is pending the job is not re-claimed.
        assert queue.claim_next(session, job_types={"embed"}) is None

    # One tick short of the deadline the job is still not due ...
    queue_clock[0] = retry_at - timedelta(microseconds=1)
    with session_factory.begin() as session:
        assert queue.claim_next(session, job_types={"embed"}) is None

    # ... and once the backoff window passes it becomes claimable again.
    queue_clock[0] = retry_at
    with session_factory.begin() as session:
        reclaimed = queue.claim_next(session, job_types={"embed"})
        assert reclaimed is not None and reclaimed.id == job.id


def test_fail_backoff_delay_is_capped(queue_clock):
    """L4: the exponential backoff must be clamped so a high retry_count cannot
    overflow timedelta (datetime + timedelta raises past ~year 9999)."""
    session_factory = make_session_factory()
    queue = JobQueueService()

    with session_factory.begin() as session:
        workspace = seed_default_workspace(session)
        queue.enqueue(
            session=session,
            workspace_id=workspace.id,
            job_type="embed",
            trigger_type="content_version",
            idempotency_key="cap-1",
        )
        claimed = queue.claim_next(session, job_types={"embed"})
        # Push retry_count high so the raw exponential (30 * 2**19) would be
        # ~15M seconds; the cap must keep next_attempt_at within MAX backoff.
        claimed.retry_count = 19
        before = queue_clock[0]
        queue.fail(session, claimed, "rate limited", max_retries=50)
        delay = (claimed.next_attempt_at - before).total_seconds()

    assert claimed.status == "queued"
    # The queue's clock does not move between ``before`` and fail(), so the
    # delay is the backoff itself: no allowance for elapsed time is needed.
    assert delay <= MAX_RETRY_BACKOFF_SECONDS


def test_reclaim_requeue_is_immediately_claimable():
    # The lease (600s) already throttles a wedging job, so the lease-reclaim
    # path does NOT add retry backoff — a crash-recovered job retries promptly.
    # Backoff applies only to the fail() path (rate-limited providers).
    session_factory = make_session_factory()
    queue = JobQueueService()

    with session_factory.begin() as session:
        workspace = seed_default_workspace(session)
        queue.enqueue(
            session=session,
            workspace_id=workspace.id,
            job_type="embed",
            trigger_type="content_version",
            idempotency_key="doc-2",
        )
        claimed = queue.claim_next(session, job_types={"embed"})
        assert claimed is not None
        # Make the lease look expired so reclaim requeues it.
        claimed.started_at = utc_now() - timedelta(seconds=3600)
        session.flush()

        reclaimed = queue.reclaim_stale_jobs(
            session, workspace_id=workspace.id, lease_timeout_seconds=600
        )
        assert len(reclaimed) == 1
        assert reclaimed[0].status == "queued"
        assert reclaimed[0].next_attempt_at is None
        # Immediately claimable again (no backoff penalty on crash recovery).
        assert queue.claim_next(session, job_types={"embed"}) is not None


def test_job_queue_enqueue_converges_on_winner_when_check_misses_it(monkeypatch):
    """enqueue() is check-then-insert; when a concurrent transaction wins the
    insert between the check and the flush, the unique-constraint conflict
    must converge on the winning row instead of raising IntegrityError into
    (and rolling back) the caller's transaction. Simulated by blinding the
    first idempotency-key lookup while the winner row already exists."""
    session_factory = make_session_factory()
    queue = JobQueueService()

    real_get = JobQueueService.get_by_idempotency_key
    lookups = {"count": 0}

    def _blind_first_lookup(self, session, workspace_id, job_type, idempotency_key):
        lookups["count"] += 1
        if lookups["count"] == 1:
            return None
        return real_get(
            self,
            session=session,
            workspace_id=workspace_id,
            job_type=job_type,
            idempotency_key=idempotency_key,
        )

    monkeypatch.setattr(JobQueueService, "get_by_idempotency_key", _blind_first_lookup)

    with session_factory.begin() as session:
        workspace = seed_default_workspace(session)
        winner = Job(
            workspace_id=workspace.id,
            job_type="library_discovery",
            trigger_type="manual",
            idempotency_key="enqueue-race-key",
        )
        session.add(winner)
        session.flush()
        job = queue.enqueue(
            session=session,
            workspace_id=workspace.id,
            job_type="library_discovery",
            trigger_type="manual",
            idempotency_key="enqueue-race-key",
        )
        # The caller's transaction survives the conflict and can keep writing.
        session.add(
            Job(
                workspace_id=workspace.id,
                job_type="library_discovery",
                trigger_type="manual",
                idempotency_key="enqueue-race-key-followup",
            )
        )
        session.flush()

    with session_factory.begin() as session:
        race_jobs = session.scalars(
            select(Job).where(Job.idempotency_key == "enqueue-race-key")
        ).all()
        followup = session.scalar(
            select(Job).where(Job.idempotency_key == "enqueue-race-key-followup")
        )

    assert lookups["count"] == 2
    assert job.id == winner.id
    assert len(race_jobs) == 1
    assert followup is not None


def test_job_queue_claim_and_complete_lifecycle():
    session_factory = make_session_factory()
    queue = JobQueueService()

    with session_factory.begin() as session:
        workspace = seed_default_workspace(session)
        queue.enqueue(
            session=session,
            workspace_id=workspace.id,
            job_type="extraction",
            trigger_type="manual",
            idempotency_key="source-version-1",
        )
        claimed = queue.claim_next(session=session, workspace_id=workspace.id)

    assert claimed is not None
    assert claimed.status == "running"
    assert claimed.started_at is not None

    with session_factory.begin() as session:
        assert queue.claim_next(session=session, workspace_id=workspace.id) is None
        job = session.get(Job, claimed.id)
        queue.complete(session=session, job=job)

    with session_factory.begin() as session:
        completed = session.get(Job, claimed.id)

    assert completed.status == "completed"
    assert completed.progress == 100
    assert completed.completed_at is not None


def test_sqlite_engine_uses_immediate_transactions(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'immediate.db'}")
    connection = engine.raw_connection()
    try:
        assert connection.isolation_level == "IMMEDIATE"
    finally:
        connection.close()
        engine.dispose()


def test_job_queue_fail_retries_until_max_attempts():
    session_factory = make_session_factory()
    queue = JobQueueService()

    with session_factory.begin() as session:
        workspace = seed_default_workspace(session)
        job = queue.enqueue(
            session=session,
            workspace_id=workspace.id,
            job_type="extraction",
            trigger_type="manual",
            idempotency_key="source-version-1",
        )
        claimed = queue.claim_next(session=session, workspace_id=workspace.id)
        assert claimed.id == job.id
        queue.fail(session=session, job=claimed, error="parser failed", max_retries=2)

    with session_factory.begin() as session:
        retry = session.get(Job, job.id)
        assert retry.status == "queued"
        assert retry.retry_count == 1
        retry.next_attempt_at = None  # elapse the retry backoff window
        session.flush()
        claimed = queue.claim_next(session=session, workspace_id=workspace.id)
        queue.fail(session=session, job=claimed, error="parser failed", max_retries=2)

    with session_factory.begin() as session:
        failed = session.get(Job, job.id)

    assert failed.status == "failed"
    assert failed.retry_count == 2
    assert failed.last_error == "parser failed"
    assert failed.completed_at is not None


def test_job_queue_fail_default_budget_is_max_total_attempts():
    """fail()'s default budget pins max-attempts semantics: a generic failure
    is requeued until the job has failed DEFAULT_JOB_MAX_ATTEMPTS times in
    total, then fails terminally."""
    session_factory = make_session_factory()
    queue = JobQueueService()

    with session_factory.begin() as session:
        workspace = seed_default_workspace(session)
        job = queue.enqueue(
            session=session,
            workspace_id=workspace.id,
            job_type="extraction",
            trigger_type="manual",
            idempotency_key="default-budget-1",
        )

    statuses = []
    for _ in range(DEFAULT_JOB_MAX_ATTEMPTS):
        with session_factory.begin() as session:
            claimed = queue.claim_next(session=session, workspace_id=workspace.id)
            assert claimed is not None
            assert claimed.id == job.id
            queue.fail(session=session, job=claimed, error="transient blip")
            statuses.append(claimed.status)
            claimed.next_attempt_at = None  # elapse the retry backoff window

    with session_factory.begin() as session:
        stored = session.get(Job, job.id)

    assert statuses == ["queued", "queued", "failed"]
    assert stored.status == "failed"
    assert stored.retry_count == DEFAULT_JOB_MAX_ATTEMPTS


def test_reclaim_stale_jobs_requeues_running_job_past_lease():
    session_factory = make_session_factory()
    queue = JobQueueService()

    with session_factory.begin() as session:
        workspace = seed_default_workspace(session)
        job = queue.enqueue(
            session=session,
            workspace_id=workspace.id,
            job_type="extraction",
            trigger_type="manual",
            idempotency_key="stale-running-1",
        )
        claimed = queue.claim_next(session=session, workspace_id=workspace.id)
        assert claimed.id == job.id
        claimed.started_at = utc_now() - timedelta(seconds=1200)
        session.add(claimed)

    with session_factory.begin() as session:
        reclaimed = queue.reclaim_stale_jobs(
            session=session,
            workspace_id=workspace.id,
            lease_timeout_seconds=600,
        )
        assert [item.id for item in reclaimed] == [job.id]

    with session_factory.begin() as session:
        stored = session.get(Job, job.id)

    assert stored.status == "queued"
    assert stored.retry_count == 1
    assert stored.stage is None
    assert stored.started_at is None
    assert "lease expired" in stored.last_error


def test_reclaim_stale_jobs_only_touches_the_named_workspace():
    """The pre-Phase-6a `reclaim_stale_jobs` had no workspace filter at all
    (plan §3 D-2 point 1) and silently reclaimed stale jobs system-wide.
    workspace_id is now required; this pins that a stale job sitting in a
    DIFFERENT workspace from the one named is left alone -- the shape the
    old code did NOT handle, not just that a same-workspace job is
    reclaimed (already covered above)."""
    session_factory = make_session_factory()
    queue = JobQueueService()

    with session_factory.begin() as session:
        target_workspace = seed_default_workspace(session)
        other_workspace = Workspace(
            slug="other-workspace",
            title="Other workspace",
            name="Other workspace",
        )
        session.add(other_workspace)
        session.flush()

        target_job = queue.enqueue(
            session=session,
            workspace_id=target_workspace.id,
            job_type="extraction",
            trigger_type="manual",
            idempotency_key="scoped-reclaim-target",
        )
        other_job = queue.enqueue(
            session=session,
            workspace_id=other_workspace.id,
            job_type="extraction",
            trigger_type="manual",
            idempotency_key="scoped-reclaim-other",
        )
        for job in (target_job, other_job):
            claimed = queue.claim_next(
                session=session, workspace_id=job.workspace_id
            )
            assert claimed.id == job.id
            claimed.started_at = utc_now() - timedelta(seconds=1200)
            session.add(claimed)

    with session_factory.begin() as session:
        reclaimed = queue.reclaim_stale_jobs(
            session=session,
            workspace_id=target_workspace.id,
            lease_timeout_seconds=600,
        )
        assert [item.id for item in reclaimed] == [target_job.id]

    with session_factory.begin() as session:
        stored_target = session.get(Job, target_job.id)
        stored_other = session.get(Job, other_job.id)

    assert stored_target.status == "queued"
    assert stored_target.retry_count == 1
    # The other workspace's equally-stale job is untouched by this call.
    assert stored_other.status == "running"
    assert stored_other.retry_count == 0


def test_reclaim_stale_jobs_leaves_fresh_running_and_queued_jobs_alone(queue_clock):
    session_factory = make_session_factory()
    queue = JobQueueService()

    with session_factory.begin() as session:
        workspace = seed_default_workspace(session)
        running = queue.enqueue(
            session=session,
            workspace_id=workspace.id,
            job_type="extraction",
            trigger_type="manual",
            idempotency_key="fresh-running-1",
        )
        # ``claim_next`` takes the oldest job, so the one meant to be claimed
        # is stamped older than the other from the queue's own clock: their
        # model-default ``created_at`` values come from the host clock.
        running.created_at = queue_clock[0] - timedelta(seconds=1)
        queued = queue.enqueue(
            session=session,
            workspace_id=workspace.id,
            job_type="extraction",
            trigger_type="manual",
            idempotency_key="still-queued-1",
        )
        queued.created_at = queue_clock[0]
        claimed = queue.claim_next(session=session, workspace_id=workspace.id)
        assert claimed.id == running.id

    with session_factory.begin() as session:
        reclaimed = queue.reclaim_stale_jobs(
            session=session,
            workspace_id=workspace.id,
            lease_timeout_seconds=600,
        )
        assert reclaimed == []

    with session_factory.begin() as session:
        stored_running = session.get(Job, running.id)
        stored_queued = session.get(Job, queued.id)

    assert stored_running.status == "running"
    assert stored_running.retry_count == 0
    assert stored_running.started_at is not None
    assert stored_queued.status == "queued"
    assert stored_queued.retry_count == 0


def test_reclaim_stale_jobs_fails_terminally_when_retry_budget_exhausted():
    session_factory = make_session_factory()
    queue = JobQueueService()

    with session_factory.begin() as session:
        workspace = seed_default_workspace(session)
        job = queue.enqueue(
            session=session,
            workspace_id=workspace.id,
            job_type="extraction",
            trigger_type="manual",
            idempotency_key="stale-exhausted-1",
        )
        claimed = queue.claim_next(session=session, workspace_id=workspace.id)
        assert claimed.id == job.id
        claimed.started_at = utc_now() - timedelta(seconds=1200)
        claimed.retry_count = 2
        session.add(claimed)

    with session_factory.begin() as session:
        reclaimed = queue.reclaim_stale_jobs(
            session=session,
            workspace_id=workspace.id,
            lease_timeout_seconds=600,
            max_retries=3,
        )
        assert [item.id for item in reclaimed] == [job.id]

    with session_factory.begin() as session:
        stored = session.get(Job, job.id)

    assert stored.status == "failed"
    assert stored.retry_count == 3
    assert stored.stage is None
    assert stored.completed_at is not None
    assert "lease expired" in stored.last_error


def test_calculate_retry_delay_bounded_jitter_and_cap():
    """Verify equal-jitter bounds, nonzero lower bound, and 3600s hard cap.

    Equal-jitter formula:
      base_delay = min(
          MAX_RETRY_BACKOFF_SECONDS,
          RETRY_BACKOFF_BASE_SECONDS * (2 ** (retry_count - 1)),
      )
      half_delay = base_delay / 2.0
      delay is in [half_delay, base_delay]
    """
    # Attempt 1: base_delay = 30.0s, half_delay = 15.0s -> range [15.0, 30.0]
    for _ in range(20):
        d1 = calculate_retry_delay(1)
        assert 15.0 <= d1 <= 30.0

    # Attempt 2: base_delay = 60.0s, half_delay = 30.0s -> range [30.0, 60.0]
    for _ in range(20):
        d2 = calculate_retry_delay(2)
        assert 30.0 <= d2 <= 60.0

    # Attempt 3: base_delay = 120.0s, half_delay = 60.0s -> range [60.0, 120.0]
    for _ in range(20):
        d3 = calculate_retry_delay(3)
        assert 60.0 <= d3 <= 120.0

    # Capped attempt (retry_count=20): base_delay = 3600.0s, half_delay = 1800.0s
    for _ in range(20):
        dc = calculate_retry_delay(20)
        assert 1800.0 <= dc <= 3600.0

    # Deterministic test with seeded random generator
    rng = random.Random(42)
    expected_delay = 15.0 + rng.uniform(0, 15.0)
    rng_test = random.Random(42)
    actual_delay = calculate_retry_delay(1, rng=rng_test)
    assert actual_delay == expected_delay


def test_fail_jittered_backoff_and_terminal_attempt_no_future_retry(queue_clock):
    """Verify state transitions in fail():
    - Non-terminal failures (retry_count < max_retries) set status='queued',
      increment retry_count, and calculate a jittered next_attempt_at in range.
    - Terminal failure (retry_count >= max_retries) sets status='failed',
      sets completed_at, and clears next_attempt_at (no future retry).
    """
    session_factory = make_session_factory()
    queue = JobQueueService()

    with session_factory.begin() as session:
        workspace = seed_default_workspace(session)
        job = queue.enqueue(
            session=session,
            workspace_id=workspace.id,
            job_type="embed",
            trigger_type="content_version",
            idempotency_key="jitter-test-1",
        )

    # Attempt 1 failure
    before_1 = queue_clock[0]
    with session_factory.begin() as session:
        claimed_1 = queue.claim_next(session, job_types={"embed"})
        assert claimed_1 is not None and claimed_1.id == job.id
        queue.fail(session, claimed_1, "error 1", max_retries=2)

    with session_factory.begin() as session:
        stored_1 = session.get(Job, job.id)
        assert stored_1.status == "queued"
        assert stored_1.retry_count == 1
        assert stored_1.started_at is None
        assert stored_1.next_attempt_at is not None
        delay_1 = (stored_1.next_attempt_at - before_1).total_seconds()
        # Attempt 1 backoff must be within [15.0, 30.0] seconds
        assert 15.0 <= delay_1 <= 30.0
        retry_at = stored_1.next_attempt_at

    # Elapse backoff window for next claim
    queue_clock[0] = retry_at

    # Attempt 2 failure (terminal, max_retries=2)
    with session_factory.begin() as session:
        claimed_2 = queue.claim_next(session, job_types={"embed"})
        assert claimed_2 is not None and claimed_2.id == job.id
        queue.fail(session, claimed_2, "error 2", max_retries=2)

    with session_factory.begin() as session:
        stored_2 = session.get(Job, job.id)
        assert stored_2.status == "failed"
        assert stored_2.retry_count == 2
        assert stored_2.completed_at is not None
        assert stored_2.last_error == "error 2"
        assert stored_2.next_attempt_at is None
        # No future retry claimable
        assert queue.claim_next(session, job_types={"embed"}) is None


def test_manual_retry_requeues_failed_job_without_resetting_retry_count():
    """D-6: manual retry must NOT reset retry_count -- it stays at whatever
    fail() left it at, so the next failure hits the exhausted budget."""
    session_factory = make_session_factory()
    queue = JobQueueService()
    assert "library_discovery" not in JOB_TYPES_WITH_DOMAIN_FAILURE_STATE

    with session_factory.begin() as session:
        workspace = seed_default_workspace(session)
        job = queue.enqueue(
            session=session,
            workspace_id=workspace.id,
            job_type="library_discovery",
            trigger_type="manual",
            idempotency_key="manual-retry-1",
        )
        claimed = queue.claim_next(session=session, workspace_id=workspace.id)
        claimed.progress = 73
        queue.fail(session=session, job=claimed, error="boom", max_retries=1)

    with session_factory.begin() as session:
        failed = session.get(Job, job.id)
        assert failed.status == "failed"
        assert failed.retry_count == 1
        assert failed.progress == 73
        assert failed.last_error == "boom"
        assert failed.completed_at is not None

        retried = queue.manual_retry(
            session=session, job_id=job.id, workspace_id=workspace.id
        )
        assert retried is not None
        assert retried.status == "queued"
        assert retried.retry_count == 1  # unchanged, NOT reset to 0
        assert retried.progress == 0
        assert retried.last_error == "boom"
        assert retried.started_at is None
        assert retried.next_attempt_at is None
        assert retried.completed_at is None
        assert retried.stage is None

    # The single extra attempt is claimable immediately (no backoff applied).
    with session_factory.begin() as session:
        claimed_again = queue.claim_next(session=session, workspace_id=workspace.id)
        assert claimed_again is not None
        assert claimed_again.id == job.id
        # If this one extra attempt also fails, fail() sees retry_count=1
        # already at max_retries=1 and terminates immediately -- it does not
        # grant a fresh attempt budget.
        queue.fail(
            session=session, job=claimed_again, error="boom again", max_retries=1
        )

    with session_factory.begin() as session:
        terminal = session.get(Job, job.id)
        assert terminal.status == "failed"
        assert terminal.retry_count == 2
        assert terminal.last_error == "boom again"


def test_manual_retry_rejects_job_type_with_domain_failure_state():
    session_factory = make_session_factory()
    queue = JobQueueService()
    assert "extraction" in JOB_TYPES_WITH_DOMAIN_FAILURE_STATE

    with session_factory.begin() as session:
        workspace = seed_default_workspace(session)
        job = queue.enqueue(
            session=session,
            workspace_id=workspace.id,
            job_type="extraction",
            trigger_type="manual",
            idempotency_key="manual-retry-unsupported-1",
        )
        claimed = queue.claim_next(session=session, workspace_id=workspace.id)
        queue.fail(session=session, job=claimed, error="boom", max_retries=1)

    with session_factory.begin() as session:
        with pytest.raises(JobRetryUnsupportedTypeError) as excinfo:
            queue.manual_retry(
                session=session, job_id=job.id, workspace_id=workspace.id
            )
        assert excinfo.value.job_type == "extraction"

    # Rejected: the job row must be untouched, still terminally failed.
    with session_factory.begin() as session:
        stored = session.get(Job, job.id)
        assert stored.status == "failed"


def test_manual_retry_returns_none_for_a_job_that_is_not_failed():
    session_factory = make_session_factory()
    queue = JobQueueService()

    with session_factory.begin() as session:
        workspace = seed_default_workspace(session)
        job = queue.enqueue(
            session=session,
            workspace_id=workspace.id,
            job_type="library_discovery",
            trigger_type="manual",
            idempotency_key="manual-retry-not-failed-1",
        )

    with session_factory.begin() as session:
        # Still "queued" -- never claimed or failed.
        assert (
            queue.manual_retry(
                session=session, job_id=job.id, workspace_id=workspace.id
            )
            is None
        )

    with session_factory.begin() as session:
        # A never-existing id also returns None rather than raising.
        assert (
            queue.manual_retry(
                session=session, job_id=uuid4(), workspace_id=workspace.id
            )
            is None
        )


def test_manual_retry_returns_none_for_a_workspace_id_that_does_not_own_the_job():
    """Epic-2 Phase 6c: manual_retry must refuse a workspace_id/job_id pair
    that does not match, even though ``session`` alone cannot tell the two
    workspaces apart in this single-DB fixture -- the check has to be an
    explicit comparison, not something the caller gets for free from
    session_for routing to a different physical shard."""
    session_factory = make_session_factory()
    queue = JobQueueService()

    with session_factory.begin() as session:
        workspace = seed_default_workspace(session)
        job = queue.enqueue(
            session=session,
            workspace_id=workspace.id,
            job_type="library_discovery",
            trigger_type="manual",
            idempotency_key="manual-retry-wrong-workspace-1",
        )
        claimed = queue.claim_next(session=session, workspace_id=workspace.id)
        queue.fail(session=session, job=claimed, error="boom", max_retries=1)

    with session_factory.begin() as session:
        other_workspace_id = uuid4()
        assert other_workspace_id != workspace.id
        assert (
            queue.manual_retry(
                session=session, job_id=job.id, workspace_id=other_workspace_id
            )
            is None
        )

    # Untouched: still terminally failed, not silently requeued under the
    # wrong workspace's authority.
    with session_factory.begin() as session:
        stored = session.get(Job, job.id)
        assert stored.status == "failed"
        assert stored.retry_count == 1

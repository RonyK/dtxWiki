"""Real two-session PostgreSQL lock-order coverage for the join lanes.

`request_join`, `decide_join_request`, and `cancel_join` must all take
Workspace -> User -> WorkspaceJoinRequest. SQLite cannot prove this: it
serializes writers behind one database-wide lock, so an ABBA inversion between
two of these lanes is invisible there and every SQLite test stays green. These
tests therefore run only against a dedicated PostgreSQL database and are
skipped, never silently passed, when one is not configured.
"""

import os
import threading
import time
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.exc import DBAPIError, OperationalError

from backend.app.db import make_session_factory
from backend.models.base import Base, utc_now
from backend.models.user import User
from backend.models.workspace import Workspace
from backend.models.workspace_join import WorkspaceJoinRequest
from backend.services.workspace_join_service import WorkspaceJoinService
from backend.tests.utils.postgres import drop_reflected_postgres_metadata

# Every test here reflects and drops the tables of whatever PostgreSQL database
# it is pointed at, and CI aims several of these files at one database, so they
# must not run beside each other under `-n`. The mark belongs in the module
# rather than in a conftest hook: xdist reads the group off the item while
# building its node id, before pytest_collection_modifyitems runs, so a mark
# added there arrives too late to reach the scheduler.
pytestmark = pytest.mark.xdist_group("postgres")

# A lane that blocks forever is as much a defect as one that deadlocks, so
# every lock wait in this module is bounded and a timeout fails the test.
LOCK_TIMEOUT_MS = 5_000
JOIN_TIMEOUT_SECONDS = 30.0


def _postgres_url() -> str | None:
    value = os.getenv("DTXWIKI_POSTGRES_CONCURRENCY_TEST_DATABASE_URL")
    return value if value and value.startswith("postgresql") else None


if os.getenv("DTXWIKI_REQUIRE_POSTGRES_CONCURRENCY") == "1" and _postgres_url() is None:
    # CI sets this. A misconfigured URL would otherwise turn the only coverage
    # that can observe the join deadlock into a silent skip, which reads as a
    # pass. Fail collection loudly instead.
    raise RuntimeError(
        "DTXWIKI_REQUIRE_POSTGRES_CONCURRENCY=1 but "
        "DTXWIKI_POSTGRES_CONCURRENCY_TEST_DATABASE_URL is unset or is not a "
        "postgresql URL; the join lock-order suite must not skip here"
    )


requires_postgres = pytest.mark.skipif(
    _postgres_url() is None,
    reason=(
        "requires a dedicated PostgreSQL database in "
        "DTXWIKI_POSTGRES_CONCURRENCY_TEST_DATABASE_URL; SQLite cannot prove "
        "lock ordering"
    ),
)


def _await_blocked_session(connection, *, timeout: float = JOIN_TIMEOUT_SECONDS):
    """Block until another backend is waiting on a lock, or fail the test."""

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        # PostgreSQL caches cumulative-statistics snapshots for the current
        # transaction. The holder transaction starts before the worker opens
        # its connection, so clear that snapshot or pg_stat_activity can keep
        # returning the pre-worker view for the whole probe.
        connection.execute(text("SELECT pg_stat_clear_snapshot()"))
        waiting = connection.execute(
            text(
                "SELECT count(*) FROM pg_stat_activity "
                "WHERE wait_event_type = 'Lock' AND pid <> pg_backend_pid()"
            )
        ).scalar_one()
        if waiting:
            return
        time.sleep(0.05)
    pytest.fail("no session ever blocked on a lock; the probe would be vacuous")


@pytest.fixture
def postgres_join_fixture():
    database_url = _postgres_url()
    assert database_url is not None
    engine = create_engine(database_url)
    drop_reflected_postgres_metadata(engine)
    Base.metadata.create_all(engine)
    session_factory = make_session_factory(engine)
    with session_factory.begin() as session:
        workspace = Workspace(
            slug=f"join-lock-{uuid4().hex[:8]}",
            title="Join Lock Order",
            name="Join Lock Order",
            visibility="team",
            join_policy="request",
        )
        session.add(workspace)
        session.flush()
        requester = User(
            email=f"requester-{uuid4().hex[:8]}@example.test",
            password_hash="x",
            display_name="Requester",
            status="active",
        )
        session.add(requester)
        session.flush()
        now = utc_now()
        session.add(
            WorkspaceJoinRequest(
                workspace_id=workspace.id,
                requester_user_id=requester.id,
                status="pending",
                attempt_no=1,
                requested_at=now,
                updated_at=now,
            )
        )
        ids = (workspace.id, requester.id)
    try:
        yield engine, session_factory, ids
    finally:
        drop_reflected_postgres_metadata(engine)
        engine.dispose()


@requires_postgres
def test_cancel_join_takes_workspace_before_user(postgres_join_fixture):
    """Prove the acquisition order with a real competing PostgreSQL session.

    A holder session takes the Workspace row, then `cancel_join` runs. If the
    service still took the requester User first (the pre-fix order) it would
    hold User while waiting on the holder's Workspace, and the holder's own
    request for User would close an ABBA cycle that PostgreSQL resolves by
    killing a transaction. With the corrected order `cancel_join` owns nothing
    while it waits, so the holder acquires User immediately and no deadlock is
    possible.
    """

    engine, session_factory, (workspace_id, requester_id) = postgres_join_fixture
    service = WorkspaceJoinService(session_factory=session_factory)
    cancel_error: list[BaseException] = []
    started = threading.Event()

    def cancel() -> None:
        started.set()
        try:
            service.cancel_join(
                workspace_id=workspace_id,
                requester_user_id=requester_id,
            )
        except BaseException as exc:  # reported to the main thread
            cancel_error.append(exc)

    with engine.connect() as holder:
        holder.execute(text(f"SET lock_timeout = {LOCK_TIMEOUT_MS}"))
        holder.execute(
            text("SELECT id FROM workspace WHERE id = :id FOR UPDATE"),
            {"id": workspace_id},
        )
        worker = threading.Thread(target=cancel, name="cancel-join")
        worker.start()
        assert started.wait(timeout=JOIN_TIMEOUT_SECONDS)
        # Wait for cancel_join to actually block on the workspace row rather
        # than sleeping a guessed interval. Both orders end up parked there, so
        # this is the point where the buggy order is holding the User row and
        # the correct order is holding nothing. Polling the real wait state
        # keeps the probe below from passing vacuously on a slow runner.
        _await_blocked_session(holder)

        try:
            holder.execute(
                text("SELECT id FROM users WHERE id = :id FOR UPDATE"),
                {"id": requester_id},
            )
        except (OperationalError, DBAPIError) as exc:
            pytest.fail(
                "cancel_join locked the requester before the workspace, so the "
                f"holder could not take the User row: {exc}"
            )
        holder.commit()

    worker.join(timeout=JOIN_TIMEOUT_SECONDS)
    assert not worker.is_alive(), "cancel_join did not finish within the bound"
    assert not cancel_error, f"cancel_join failed: {cancel_error[0]!r}"

    with session_factory() as session:
        request = session.scalar(
            select(WorkspaceJoinRequest).where(
                WorkspaceJoinRequest.workspace_id == workspace_id,
                WorkspaceJoinRequest.requester_user_id == requester_id,
            )
        )
        assert request is not None
        assert request.status == "cancelled"


@requires_postgres
def test_concurrent_request_and_cancel_reach_a_deterministic_state(
    postgres_join_fixture,
):
    """Run both lanes at once: both must succeed, and one terminal state wins.

    Every exception is a failure here, with no whitelist. Once both lanes take
    Workspace -> User -> WorkspaceJoinRequest, the workspace row lock fully
    serializes them, so whichever thread wins that lock runs to completion
    before the other begins. Neither ordering has a losing branch: cancel-then-
    request finds a cancelled row and claims a new attempt, and request-then-
    cancel finds the seeded pending row and returns it unchanged before cancel
    resolves it.

    That includes `WorkspaceJoinRequestStaleError`, which is the one exception
    that might look like acceptable race noise. It cannot legitimately fire in
    this scenario: `_request_join_once` reads the row with `with_for_update()`
    and only then runs its compare-and-swap on that same locked row, so the
    swap cannot lose while the lock order holds. If it ever appears here, the
    two lanes are not serializing the way the lock order promises, which is
    precisely the regression this module exists to catch -- so it is reported,
    not tolerated.
    """

    engine, session_factory, (workspace_id, requester_id) = postgres_join_fixture
    service = WorkspaceJoinService(session_factory=session_factory)
    barrier = threading.Barrier(2, timeout=JOIN_TIMEOUT_SECONDS)
    # BaseException, not Exception: a barrier timeout or a thread torn down by
    # anything else must surface as a failure rather than leave `failures`
    # empty while the row-state assertion below still passes.
    failures: list[tuple[str, BaseException]] = []

    def run(name: str, operation) -> None:
        try:
            barrier.wait()
            operation(
                workspace_id=workspace_id,
                requester_user_id=requester_id,
            )
        except BaseException as exc:  # reported to the main thread
            failures.append((name, exc))

    threads = [
        threading.Thread(
            target=run, args=("cancel", service.cancel_join), name="cancel"
        ),
        threading.Thread(
            target=run, args=("request", service.request_join), name="request"
        ),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=JOIN_TIMEOUT_SECONDS)
        assert not thread.is_alive(), f"{thread.name} did not finish within the bound"

    rendered_failures = "; ".join(
        f"{name} raised {type(exc).__name__}: {exc}" for name, exc in failures
    )
    assert not failures, (
        "both join lanes must succeed under the corrected lock order, so every "
        f"exception is a regression: {rendered_failures}"
    )

    with session_factory() as session:
        requests = session.scalars(
            select(WorkspaceJoinRequest).where(
                WorkspaceJoinRequest.workspace_id == workspace_id,
                WorkspaceJoinRequest.requester_user_id == requester_id,
            )
        ).all()
    # The unique pair constraint means exactly one row survives whichever lane
    # committed last, and it is always one of the two terminal states.
    assert len(requests) == 1
    assert requests[0].status in {"pending", "cancelled"}

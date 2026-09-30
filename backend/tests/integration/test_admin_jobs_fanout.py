"""Epic-2 Phase 6a (doc/design/workspace-database-separation-plan.md §4):
the server-admin job listing/summary fan out across the workspace roster
instead of running one cross-workspace query, because once Job is
workspace-plane data a single query has nothing left to read.

Unlike test_job_visibility_api.py (single-DB fixture, `page_service`), these
tests need genuinely different shards per workspace to prove the fan-out is
real and not just "the same DB, queried once" -- so they build a
MultiShardProvider the way test_cloud_composition.py's SplitProvider proves
plane routing, extended to route more than one workspace_id to more than one
data DB (SplitProvider itself only has one control DB + one data DB).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from backend.app.container import DatabaseProvider, assemble
from backend.app.db import make_engine, make_session_factory
from backend.app.main import create_app
from backend.app.settings import Settings
from backend.models.base import Base
from backend.models.job import Job
from backend.models.user import User
from backend.models.workspace import Workspace
from backend.services import server_admin_service
from backend.services.page_service import PageService
from backend.tests.conftest import authenticate_test_client

_BASE_TIME = datetime(2026, 1, 1, tzinfo=UTC)


class _PoisonSessionFactory:
    """Stands in for an unreachable shard: raises the moment it is opened,
    the same failure shape a real dropped connection would produce."""

    def __call__(self):
        raise SQLAlchemyError("shard unreachable (test double)")


class MultiShardProvider(DatabaseProvider):
    """DatabaseProvider double routing more than one workspace_id to more
    than one *distinct* data DB -- test_cloud_composition.py's SplitProvider
    only has one data DB behind every workspace_id, which cannot prove a
    fan-out actually visits N different shards rather than querying the same
    DB N times. Unregistered workspace ids (e.g. the personal workspace
    `authenticate_test_client`'s signup creates) fall back to the control
    factory, mirroring the base DatabaseProvider's single-factory default.
    """

    def __init__(
        self,
        control_factory: sessionmaker[Session],
        shard_factories: dict[UUID, sessionmaker[Session]],
        *,
        poison_ids: frozenset[UUID] = frozenset(),
    ) -> None:
        super().__init__(None, session_factory=control_factory)
        self._shard_factories = shard_factories
        self._poison_ids = poison_ids
        self.session_for_calls: list[UUID] = []

    def session_for(self, workspace_id):
        self.session_for_calls.append(workspace_id)
        if workspace_id in self._poison_ids:
            return _PoisonSessionFactory()
        return self._shard_factories.get(workspace_id, self.session_factory)

    def heal(self, workspace_id: UUID, factory: sessionmaker[Session]) -> None:
        """Bring a poisoned shard back mid-walk.

        The failure that matters for pagination is not "a shard is down" but
        "a shard was down for one page and is up for the next", so a test
        needs to flip that state between requests.
        """
        self._poison_ids = frozenset(self._poison_ids - {workspace_id})
        self._shard_factories[workspace_id] = factory


def _make_shard(tmp_path, name: str) -> sessionmaker[Session]:
    engine = make_engine(f"sqlite+pysqlite:///{tmp_path / name}.sqlite")
    Base.metadata.create_all(engine)
    return make_session_factory(engine)


def _register_workspace(
    factory, *, workspace_id: UUID, slug: str
) -> None:
    """Insert a Workspace row into ``factory``'s DB.

    Job.workspace_id carries a real FK to ``workspace.id`` (backend/models/
    job.py), and every shard DB gets the full schema (Base.metadata.create_
    all), so a Job row inserted into a shard needs a matching Workspace row
    in that *same* shard, not just in the control DB -- mirrors the pattern
    test_cloud_composition.py's split-provider fixtures already use.
    """
    with factory.begin() as session:
        session.add(
            Workspace(id=workspace_id, slug=slug, title=slug, name=slug)
        )


def _register_reachable_workspace(
    control_factory, shard_factory, *, workspace_id: UUID, slug: str
) -> None:
    """The common case below: a workspace whose shard is actually queried,
    so it needs the Workspace row in both the control DB (the roster read)
    and its own shard DB (the Job FK) -- see _register_workspace's docstring.
    A poisoned/unreachable workspace, by contrast, only ever needs the
    control-DB row: its shard is never really opened.
    """
    _register_workspace(control_factory, workspace_id=workspace_id, slug=slug)
    _register_workspace(shard_factory, workspace_id=workspace_id, slug=slug)


def _insert_job(
    shard_factory,
    *,
    workspace_id: UUID,
    created_at: datetime,
    idempotency_key: str,
    job_type: str = "library_discovery",
    status: str = "queued",
) -> UUID:
    job_id = uuid4()
    with shard_factory.begin() as session:
        session.add(
            Job(
                id=job_id,
                workspace_id=workspace_id,
                job_type=job_type,
                trigger_type="manual",
                status=status,
                idempotency_key=idempotency_key,
                created_at=created_at,
            )
        )
    return job_id


def _insert_terminally_failed_job(
    shard_factory,
    *,
    workspace_id: UUID,
    created_at: datetime,
    idempotency_key: str,
    job_type: str = "library_discovery",
    error: str = "boom",
) -> UUID:
    """Mirrors JobQueueService.fail()'s terminal shape (retry_count/
    last_error/completed_at set, status="failed") without going through
    claim/fail -- these fan-out tests only need a job already sitting in the
    state admin_retry_job/manual_retry act on."""
    job_id = _insert_job(
        shard_factory,
        workspace_id=workspace_id,
        created_at=created_at,
        idempotency_key=idempotency_key,
        job_type=job_type,
        status="failed",
    )
    with shard_factory.begin() as session:
        job = session.get(Job, job_id)
        job.retry_count = 1
        job.last_error = error
        job.completed_at = created_at
    return job_id


def _build_admin_client(
    tmp_path,
    *,
    provider: DatabaseProvider,
    control_factory,
    email: str = "fanout-admin@example.com",
) -> TestClient:
    settings = Settings(
        database_url="sqlite+pysqlite:///:memory:",
        session_secret="fanout-test-secret",
    )
    container = assemble(settings, db=provider)
    app = create_app(
        settings=settings,
        page_service=PageService(control_factory),
        container=container,
        bootstrap_on_startup=False,
    )
    client = TestClient(app)
    authenticate_test_client(client, email=email)
    with control_factory.begin() as session:
        user_id = session.scalar(select(User.id).where(User.email == email))
        server_admin_service.grant_server_admin(session, user_id, granted_by="test")
    return client


def test_admin_list_jobs_includes_a_job_from_a_second_workspace(tmp_path):
    """Highest-value fan-out check: a job that lives only in workspace B's
    shard is not visible to a single-query control-plane read, but must
    appear in the cross-workspace admin listing."""
    control_factory = _make_shard(tmp_path, "control")
    shard_a = _make_shard(tmp_path, "shard-a")
    shard_b = _make_shard(tmp_path, "shard-b")
    workspace_a, workspace_b = uuid4(), uuid4()
    _register_reachable_workspace(
        control_factory, shard_a, workspace_id=workspace_a, slug="alpha"
    )
    _register_reachable_workspace(
        control_factory, shard_b, workspace_id=workspace_b, slug="bravo"
    )
    job_b = _insert_job(
        shard_b,
        workspace_id=workspace_b,
        created_at=_BASE_TIME,
        idempotency_key="bravo-1",
    )
    provider = MultiShardProvider(
        control_factory, {workspace_a: shard_a, workspace_b: shard_b}
    )
    client = _build_admin_client(
        tmp_path, provider=provider, control_factory=control_factory
    )

    response = client.get("/api/v1/admin/jobs")

    assert response.status_code == 200, response.text
    body = response.json()
    ids = {item["id"] for item in body["data"]["items"]}
    assert str(job_b) in ids
    workspace_b_item = next(
        item for item in body["data"]["items"] if item["id"] == str(job_b)
    )
    assert workspace_b_item["workspace_slug"] == "bravo"
    assert body["data"]["unreachable_workspace_ids"] == []
    assert body["envelope"]["warnings"] == []


def test_admin_jobs_summary_counts_jobs_from_more_than_one_workspace(tmp_path):
    control_factory = _make_shard(tmp_path, "control")
    shard_a = _make_shard(tmp_path, "shard-a")
    shard_b = _make_shard(tmp_path, "shard-b")
    workspace_a, workspace_b = uuid4(), uuid4()
    _register_reachable_workspace(
        control_factory, shard_a, workspace_id=workspace_a, slug="alpha"
    )
    _register_reachable_workspace(
        control_factory, shard_b, workspace_id=workspace_b, slug="bravo"
    )
    _insert_job(
        shard_a,
        workspace_id=workspace_a,
        created_at=_BASE_TIME,
        idempotency_key="alpha-1",
        status="queued",
    )
    _insert_job(
        shard_a,
        workspace_id=workspace_a,
        created_at=_BASE_TIME,
        idempotency_key="alpha-2",
        status="failed",
    )
    _insert_job(
        shard_b,
        workspace_id=workspace_b,
        created_at=_BASE_TIME,
        idempotency_key="bravo-1",
        status="queued",
    )
    provider = MultiShardProvider(
        control_factory, {workspace_a: shard_a, workspace_b: shard_b}
    )
    client = _build_admin_client(
        tmp_path, provider=provider, control_factory=control_factory
    )

    response = client.get("/api/v1/admin/jobs/summary")

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    # Not derived from the same summation the implementation performs --
    # hand-computed from the three rows inserted above, split across shards.
    assert data["queued_count"] == 2
    assert data["failed_count"] == 1
    assert data["unreachable_workspace_ids"] == []


def test_admin_list_jobs_pages_across_a_shard_boundary_without_duplicates_or_gaps(
    tmp_path,
):
    """The case a fan-out cursor can get wrong: a page boundary that falls
    in the middle of the globally-merged order, spanning two shards."""
    control_factory = _make_shard(tmp_path, "control")
    shard_a = _make_shard(tmp_path, "shard-a")
    shard_b = _make_shard(tmp_path, "shard-b")
    workspace_a, workspace_b = uuid4(), uuid4()
    _register_reachable_workspace(
        control_factory, shard_a, workspace_id=workspace_a, slug="alpha"
    )
    _register_reachable_workspace(
        control_factory, shard_b, workspace_id=workspace_b, slug="bravo"
    )

    # Interleaved by construction: global desc order by created_at is
    # a1(6) b1(5) a2(4) b2(3) a3(2) b3(1) -- every 3-item page boundary
    # crosses from one shard to the other.
    a1 = _insert_job(
        shard_a,
        workspace_id=workspace_a,
        created_at=_BASE_TIME + timedelta(minutes=6),
        idempotency_key="a-1",
    )
    b1 = _insert_job(
        shard_b,
        workspace_id=workspace_b,
        created_at=_BASE_TIME + timedelta(minutes=5),
        idempotency_key="b-1",
    )
    a2 = _insert_job(
        shard_a,
        workspace_id=workspace_a,
        created_at=_BASE_TIME + timedelta(minutes=4),
        idempotency_key="a-2",
    )
    b2 = _insert_job(
        shard_b,
        workspace_id=workspace_b,
        created_at=_BASE_TIME + timedelta(minutes=3),
        idempotency_key="b-2",
    )
    a3 = _insert_job(
        shard_a,
        workspace_id=workspace_a,
        created_at=_BASE_TIME + timedelta(minutes=2),
        idempotency_key="a-3",
    )
    b3 = _insert_job(
        shard_b,
        workspace_id=workspace_b,
        created_at=_BASE_TIME + timedelta(minutes=1),
        idempotency_key="b-3",
    )
    expected_order = [a1, b1, a2, b2, a3, b3]

    provider = MultiShardProvider(
        control_factory, {workspace_a: shard_a, workspace_b: shard_b}
    )
    client = _build_admin_client(
        tmp_path, provider=provider, control_factory=control_factory
    )

    page1 = client.get("/api/v1/admin/jobs", params={"limit": 3})
    assert page1.status_code == 200, page1.text
    page1_body = page1.json()["data"]
    page1_ids = [UUID(item["id"]) for item in page1_body["items"]]
    assert page1_ids == expected_order[:3]
    assert page1_body["next_cursor"] is not None

    page2 = client.get(
        "/api/v1/admin/jobs",
        params={"limit": 3, "cursor": page1_body["next_cursor"]},
    )
    assert page2.status_code == 200, page2.text
    page2_body = page2.json()["data"]
    page2_ids = [UUID(item["id"]) for item in page2_body["items"]]
    assert page2_ids == expected_order[3:]
    assert page2_body["next_cursor"] is None

    # No duplicates and no gaps across the boundary: the two pages together
    # are exactly the six inserted jobs, each exactly once.
    assert page1_ids + page2_ids == expected_order
    assert len(set(page1_ids) | set(page2_ids)) == 6


def test_admin_list_jobs_flags_unreachable_workspace_not_500_or_short_list(
    tmp_path,
):
    control_factory = _make_shard(tmp_path, "control")
    shard_a = _make_shard(tmp_path, "shard-a")
    workspace_a, workspace_broken = uuid4(), uuid4()
    _register_reachable_workspace(
        control_factory, shard_a, workspace_id=workspace_a, slug="alpha"
    )
    # Poisoned shard: no shard DB of its own, only the control-plane roster
    # row -- session_for(workspace_broken) raises before anything would try
    # to read a Job table that does not exist for it.
    _register_workspace(
        control_factory, workspace_id=workspace_broken, slug="broken"
    )
    job_a = _insert_job(
        shard_a,
        workspace_id=workspace_a,
        created_at=_BASE_TIME,
        idempotency_key="alpha-1",
    )
    provider = MultiShardProvider(
        control_factory,
        {workspace_a: shard_a},
        poison_ids=frozenset({workspace_broken}),
    )
    client = _build_admin_client(
        tmp_path, provider=provider, control_factory=control_factory
    )

    response = client.get("/api/v1/admin/jobs")

    assert response.status_code == 200, response.text
    body = response.json()
    # Not a 500, and not silently short: the reachable workspace's job is
    # still there, and the gap is named rather than swallowed.
    ids = {item["id"] for item in body["data"]["items"]}
    assert str(job_a) in ids
    assert body["data"]["unreachable_workspace_ids"] == [str(workspace_broken)]
    assert [w["code"] for w in body["envelope"]["warnings"]] == [
        "job_fanout_incomplete"
    ]


def test_admin_jobs_summary_flags_unreachable_workspace_not_500_or_short_count(
    tmp_path,
):
    control_factory = _make_shard(tmp_path, "control")
    shard_a = _make_shard(tmp_path, "shard-a")
    workspace_a, workspace_broken = uuid4(), uuid4()
    _register_reachable_workspace(
        control_factory, shard_a, workspace_id=workspace_a, slug="alpha"
    )
    _register_workspace(
        control_factory, workspace_id=workspace_broken, slug="broken"
    )
    _insert_job(
        shard_a,
        workspace_id=workspace_a,
        created_at=_BASE_TIME,
        idempotency_key="alpha-1",
        status="queued",
    )
    provider = MultiShardProvider(
        control_factory,
        {workspace_a: shard_a},
        poison_ids=frozenset({workspace_broken}),
    )
    client = _build_admin_client(
        tmp_path, provider=provider, control_factory=control_factory
    )

    response = client.get("/api/v1/admin/jobs/summary")

    assert response.status_code == 200, response.text
    body = response.json()
    # The reachable workspace's count still shows -- this is a partial
    # result, not a withheld one -- but it is flagged, not silent.
    assert body["data"]["queued_count"] == 1
    assert body["data"]["unreachable_workspace_ids"] == [str(workspace_broken)]
    assert [w["code"] for w in body["envelope"]["warnings"]] == [
        "job_fanout_incomplete"
    ]


def test_admin_retry_job_requeues_a_failed_job_living_on_a_data_shard(tmp_path):
    """The defect this fixes: POST /admin/jobs/{id}/retry used to mutate
    through the control session_factory, which never sees a workspace's own
    shard once the DB is split. A job living only on shard B's Job table
    (never in the control DB) 404ed even though the admin listing -- proven
    above to fan out correctly -- could see it fine.

    Verifies the status flip on shard B directly via a FRESH session from
    shard_b's own factory, not by reading the endpoint's own response body or
    re-listing through the admin API: both of those would only prove the
    endpoint renders whatever manual_retry happened to return, not that the
    mutation actually landed in the database. If admin_retry_job's session
    were reverted to the control session_factory, this job (which exists
    only in shard_b, never in the control DB) would be invisible to
    session.get() there and the endpoint would 404 -- this assertion on the
    response status_code alone already catches that regression.
    """
    control_factory = _make_shard(tmp_path, "control")
    shard_b = _make_shard(tmp_path, "shard-b")
    workspace_b = uuid4()
    _register_reachable_workspace(
        control_factory, shard_b, workspace_id=workspace_b, slug="bravo"
    )
    job_b = _insert_terminally_failed_job(
        shard_b,
        workspace_id=workspace_b,
        created_at=_BASE_TIME,
        idempotency_key="bravo-retry-1",
        error="boom",
    )
    provider = MultiShardProvider(control_factory, {workspace_b: shard_b})
    client = _build_admin_client(
        tmp_path, provider=provider, control_factory=control_factory
    )

    response = client.post(
        f"/api/v1/admin/jobs/{job_b}/retry",
        params={"workspace_id": str(workspace_b)},
    )

    assert response.status_code == 200, response.text
    body = response.json()["data"]
    assert body["status"] == "queued"
    assert body["retry_count"] == 1  # unchanged, not reset (D-6)

    # Independent read-back on a fresh shard_b session -- see docstring.
    with shard_b() as verify_session:
        stored = verify_session.get(Job, job_b)
        assert stored.status == "queued"
        assert stored.retry_count == 1
        assert stored.last_error == "boom"  # preserved
        assert stored.completed_at is None

    # The control DB's own `job` table (created everywhere by
    # Base.metadata.create_all, same as every shard) must never have held
    # this row -- proves the mutation went through session_for(workspace_id)
    # and not a write that landed on both planes.
    with control_factory() as control_session:
        assert control_session.get(Job, job_b) is None


def test_admin_retry_job_missing_id_is_not_found(tmp_path):
    """A job id that exists in NO workspace's shard -- distinct from the
    wrong-workspace case below, which is a real row under a different id
    than named."""
    control_factory = _make_shard(tmp_path, "control")
    shard_a = _make_shard(tmp_path, "shard-a")
    workspace_a = uuid4()
    _register_reachable_workspace(
        control_factory, shard_a, workspace_id=workspace_a, slug="alpha"
    )
    provider = MultiShardProvider(control_factory, {workspace_a: shard_a})
    client = _build_admin_client(
        tmp_path, provider=provider, control_factory=control_factory
    )

    response = client.post(
        f"/api/v1/admin/jobs/{uuid4()}/retry",
        params={"workspace_id": str(workspace_a)},
    )

    assert response.status_code == 404, response.text
    assert response.json()["error_code"] == "job_not_found"


def test_admin_retry_job_still_refuses_domain_failure_state_type_on_a_shard(
    tmp_path,
):
    """The JOB_TYPES_WITH_DOMAIN_FAILURE_STATE guard (manual_retry) must keep
    refusing once the mutation runs on session_for(workspace_id) instead of
    the control session -- this is the check the reviewer flagged as
    load-bearing and checked inside the same transaction as the status flip,
    so it must not have been weakened by the routing change."""
    control_factory = _make_shard(tmp_path, "control")
    shard_a = _make_shard(tmp_path, "shard-a")
    workspace_a = uuid4()
    _register_reachable_workspace(
        control_factory, shard_a, workspace_id=workspace_a, slug="alpha"
    )
    job_id = _insert_terminally_failed_job(
        shard_a,
        workspace_id=workspace_a,
        created_at=_BASE_TIME,
        idempotency_key="alpha-domain-failure-1",
        job_type="extraction",
    )
    provider = MultiShardProvider(control_factory, {workspace_a: shard_a})
    client = _build_admin_client(
        tmp_path, provider=provider, control_factory=control_factory
    )

    response = client.post(
        f"/api/v1/admin/jobs/{job_id}/retry",
        params={"workspace_id": str(workspace_a)},
    )

    assert response.status_code == 422, response.text
    assert response.json()["error_code"] == "job_retry_unsupported_type"

    # Refused: the row on the shard must be untouched, still failed.
    with shard_a() as verify_session:
        stored = verify_session.get(Job, job_id)
        assert stored.status == "failed"
        assert stored.retry_count == 1


def test_admin_list_jobs_does_not_lose_rows_from_a_shard_that_recovers_mid_walk(
    tmp_path,
):
    """JOB-01: the intersection the suite never covered.

    Paging is tested with every shard healthy; partial failure is tested on a
    single page with no cursor followed. Between those two lies the defect: on
    page 1 the cursor was derived from the REACHABLE shards' rows only, so it
    advanced past the region the broken shard had never been asked to serve.
    On page 2 that shard's rows above the cursor were excluded by "< seek"
    permanently -- no later page could reach them.

    Charlie's rows are deliberately the newest in the listing, so on the
    pre-fix code they rank above the page-1 cursor and vanish for good.
    """
    control_factory = _make_shard(tmp_path, "control")
    shard_a = _make_shard(tmp_path, "shard-a")
    shard_c = _make_shard(tmp_path, "shard-c")
    workspace_a, workspace_c = uuid4(), uuid4()
    _register_reachable_workspace(
        control_factory, shard_a, workspace_id=workspace_a, slug="alpha"
    )
    # Charlie is registered in the roster and has a real shard DB, but the
    # provider is told to poison it for page 1 only.
    _register_reachable_workspace(
        control_factory, shard_c, workspace_id=workspace_c, slug="charlie"
    )

    alpha_ids = {
        _insert_job(
            shard_a,
            workspace_id=workspace_a,
            created_at=_BASE_TIME + timedelta(minutes=index),
            idempotency_key=f"alpha-{index}",
        )
        for index in range(1, 4)
    }
    # Newer than every alpha row: these are exactly the rows a forward-only
    # global cursor can never come back for.
    charlie_ids = {
        _insert_job(
            shard_c,
            workspace_id=workspace_c,
            created_at=_BASE_TIME + timedelta(minutes=index),
            idempotency_key=f"charlie-{index}",
        )
        for index in range(10, 13)
    }

    provider = MultiShardProvider(
        control_factory,
        {workspace_a: shard_a},
        poison_ids=frozenset({workspace_c}),
    )
    client = _build_admin_client(
        tmp_path, provider=provider, control_factory=control_factory
    )

    first = client.get("/api/v1/admin/jobs", params={"limit": 3})
    assert first.status_code == 200, first.text
    first_body = first.json()["data"]
    assert first_body["unreachable_workspace_ids"] == [str(workspace_c)]
    cursor = first_body["next_cursor"]
    assert cursor is not None, (
        "a page that could not reach a shard must still hand back a cursor, "
        "or the rows that shard owes become unreachable"
    )

    provider.heal(workspace_c, shard_c)

    seen = {item["id"] for item in first_body["items"]}
    for _ in range(5):
        if cursor is None:
            break
        following = client.get(
            "/api/v1/admin/jobs", params={"limit": 3, "cursor": cursor}
        )
        assert following.status_code == 200, following.text
        body = following.json()["data"]
        page_ids = [item["id"] for item in body["items"]]
        assert not (seen & set(page_ids)), (
            f"a row was served twice across pages: {seen & set(page_ids)}"
        )
        seen.update(page_ids)
        cursor = body["next_cursor"]

    expected = {str(job_id) for job_id in alpha_ids | charlie_ids}
    assert seen == expected, (
        "every row must be reachable through the walk; missing "
        f"{sorted(expected - seen)}, unexpected {sorted(seen - expected)}"
    )


def test_admin_list_jobs_keeps_a_cursor_when_a_short_page_hid_an_unreachable_shard(
    tmp_path,
):
    """The second half of JOB-01, and the quieter one.

    ``has_more`` was computed from the reachable shards alone. When they
    returned fewer rows than the limit, the listing ended with
    ``next_cursor: None`` -- so the broken shard's rows were not merely
    delayed, there was no token left with which to ask for them again. The
    response looked complete apart from an advisory warning.
    """
    control_factory = _make_shard(tmp_path, "control")
    shard_a = _make_shard(tmp_path, "shard-a")
    shard_c = _make_shard(tmp_path, "shard-c")
    workspace_a, workspace_c = uuid4(), uuid4()
    _register_reachable_workspace(
        control_factory, shard_a, workspace_id=workspace_a, slug="alpha"
    )
    _register_reachable_workspace(
        control_factory, shard_c, workspace_id=workspace_c, slug="charlie"
    )

    alpha = _insert_job(
        shard_a,
        workspace_id=workspace_a,
        created_at=_BASE_TIME,
        idempotency_key="alpha-only",
    )
    charlie = _insert_job(
        shard_c,
        workspace_id=workspace_c,
        created_at=_BASE_TIME + timedelta(minutes=5),
        idempotency_key="charlie-only",
    )

    provider = MultiShardProvider(
        control_factory,
        {workspace_a: shard_a},
        poison_ids=frozenset({workspace_c}),
    )
    client = _build_admin_client(
        tmp_path, provider=provider, control_factory=control_factory
    )

    # One reachable row against a limit of 10: the page is far from full.
    first = client.get("/api/v1/admin/jobs", params={"limit": 10}).json()["data"]
    assert [item["id"] for item in first["items"]] == [str(alpha)]
    assert first["unreachable_workspace_ids"] == [str(workspace_c)]
    assert first["next_cursor"] is not None, (
        "a short page is not proof the listing is exhausted when a shard was "
        "unreachable; without a cursor its rows can never be requested again"
    )

    provider.heal(workspace_c, shard_c)
    second = client.get(
        "/api/v1/admin/jobs", params={"limit": 10, "cursor": first["next_cursor"]}
    ).json()["data"]
    assert [item["id"] for item in second["items"]] == [str(charlie)]
    assert second["unreachable_workspace_ids"] == []

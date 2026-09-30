"""Epic-2 Phase 1 (job visibility, doc/design/workspace-database-separation-plan.md
D-6): GET /workspaces/{id}/jobs (1b), the server-admin GET /admin/jobs* surface
and POST .../retry (1c).
"""

import base64
import inspect
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from sqlalchemy import select

from backend.api import read as read_api
from backend.api.deps import WorkspaceViewerAccessDep
from backend.app.main import create_app
from backend.app.settings import Settings
from backend.models.job import Job
from backend.models.user import User
from backend.services import job_read_service as job_read_service_module
from backend.services import server_admin_service
from backend.services.job_queue import (
    JOB_TYPES_WITH_DOMAIN_FAILURE_STATE,
    JobQueueService,
)
from backend.tests.conftest import (
    authenticate_test_client,
    authenticate_test_client_in_workspace,
)


def make_client(
    page_service, email: str = "jobs-test@example.com", role: str = "owner"
) -> TestClient:
    client = TestClient(
        create_app(
            settings=Settings(
                database_url="sqlite+pysqlite:///:memory:",
                session_secret="test-session-secret",
            ),
            page_service=page_service,
        )
    )
    authenticate_test_client_in_workspace(
        client,
        page_service.session_factory,
        email=email,
        workspace_slug="default",
        role=role,
    )
    return client


def make_server_admin_client(
    page_service, email: str = "jobs-admin@example.com"
) -> TestClient:
    client = make_client(page_service, email=email)
    with page_service.session_factory.begin() as session:
        user = session.scalar(select(User).where(User.email == email))
        server_admin_service.grant_server_admin(session, user.id, granted_by="test")
    return client


def _workspace_id(client: TestClient) -> str:
    return client.get("/api/v1/workspaces").json()["data"]["current_workspace_id"]


def _enqueue(page_service, workspace_id: str, **kwargs) -> Job:
    queue = JobQueueService()
    with page_service.session_factory.begin() as session:
        return queue.enqueue(session=session, workspace_id=UUID(workspace_id), **kwargs)


def _fail_terminally(page_service, job_id, *, error: str = "boom") -> None:
    queue = JobQueueService()
    with page_service.session_factory.begin() as session:
        job = session.get(Job, job_id)
        queue.fail(session=session, job=job, error=error, max_retries=1)


def test_list_workspace_jobs_returns_jobs_and_hides_non_terminal_internal_types(
    page_service,
):
    client = make_client(page_service)
    workspace_id = _workspace_id(client)
    discovery = _enqueue(
        page_service,
        workspace_id,
        job_type="library_discovery",
        trigger_type="manual",
        idempotency_key="visible-1",
    )
    _enqueue(
        page_service,
        workspace_id,
        job_type="embed",
        trigger_type="content_version",
        idempotency_key="hidden-embed-1",
    )
    _enqueue(
        page_service,
        workspace_id,
        job_type="email_delivery",
        trigger_type="password_reset",
        idempotency_key="hidden-email-1",
    )

    response = client.get(f"/api/v1/workspaces/{workspace_id}/jobs")

    assert response.status_code == 200
    items = response.json()["data"]["items"]
    job_types = {item["job_type"] for item in items}
    assert "library_discovery" in job_types
    assert "embed" not in job_types
    assert "email_delivery" not in job_types
    listed = next(item for item in items if item["id"] == str(discovery.id))
    assert listed["status"] == "queued"
    assert listed["retryable"] is False  # not failed yet
    assert listed["lease_expired"] is False
    assert listed["workspace_slug"] is None


def test_list_workspace_jobs_surfaces_a_failed_embed_job(page_service):
    """embed is hidden while non-terminal (previous test) but must appear
    once it reaches status="failed" -- unlike email_delivery, which stays
    hidden even then (job_queue.NOISY_UNTIL_FAILED_JOB_TYPES vs
    ALWAYS_HIDDEN_JOB_TYPES)."""
    client = make_client(page_service)
    workspace_id = _workspace_id(client)
    failed_embed = _enqueue(
        page_service,
        workspace_id,
        job_type="embed",
        trigger_type="content_version",
        idempotency_key="failed-embed-1",
    )
    _fail_terminally(page_service, failed_embed.id)
    failed_email = _enqueue(
        page_service,
        workspace_id,
        job_type="email_delivery",
        trigger_type="password_reset",
        idempotency_key="failed-email-1",
    )
    _fail_terminally(page_service, failed_email.id)

    response = client.get(f"/api/v1/workspaces/{workspace_id}/jobs")

    assert response.status_code == 200
    items = response.json()["data"]["items"]
    ids = {item["id"] for item in items}
    assert str(failed_embed.id) in ids
    assert str(failed_email.id) not in ids


def test_list_workspace_jobs_status_filter_and_problems_only(page_service):
    client = make_client(page_service)
    workspace_id = _workspace_id(client)
    queued = _enqueue(
        page_service,
        workspace_id,
        job_type="library_discovery",
        trigger_type="manual",
        idempotency_key="status-filter-queued",
    )
    failed = _enqueue(
        page_service,
        workspace_id,
        job_type="feedback_resolution",
        trigger_type="manual",
        idempotency_key="status-filter-failed",
    )
    _fail_terminally(page_service, failed.id)

    queued_only = client.get(
        f"/api/v1/workspaces/{workspace_id}/jobs", params={"status": "queued"}
    )
    assert queued_only.status_code == 200
    queued_ids = {item["id"] for item in queued_only.json()["data"]["items"]}
    assert str(queued.id) in queued_ids
    assert str(failed.id) not in queued_ids

    problems = client.get(
        f"/api/v1/workspaces/{workspace_id}/jobs", params={"problems_only": True}
    )
    assert problems.status_code == 200
    problem_ids = {item["id"] for item in problems.json()["data"]["items"]}
    assert str(failed.id) in problem_ids
    assert str(queued.id) not in problem_ids
    failed_item = next(
        item
        for item in problems.json()["data"]["items"]
        if item["id"] == str(failed.id)
    )
    assert "last_error" not in failed_item
    # feedback_resolution carries no domain on_failure marker.
    assert failed_item["retryable"] is True


def test_job_apis_never_return_internal_error_text(page_service):
    workspace_client = make_client(page_service)
    workspace_id = _workspace_id(workspace_client)
    failed = _enqueue(
        page_service,
        workspace_id,
        job_type="feedback_resolution",
        trigger_type="manual",
        idempotency_key="error-boundary-failed",
    )
    # POSIX slashes deliberately: a Windows path's backslashes are re-escaped
    # in the JSON body, so the "not in response.text" assertion below could
    # never fail on a leak of the raw text.
    internal_error = "/srv/dtxwiki/private/converter.log token=secret-sentinel"
    _fail_terminally(page_service, failed.id, error=internal_error)

    workspace_response = workspace_client.get(
        f"/api/v1/workspaces/{workspace_id}/jobs",
        params={"problems_only": True},
    )

    assert workspace_response.status_code == 200
    workspace_item = next(
        item
        for item in workspace_response.json()["data"]["items"]
        if item["id"] == str(failed.id)
    )
    assert "last_error" not in workspace_item
    assert internal_error not in workspace_response.text

    admin_client = make_server_admin_client(
        page_service,
        email="jobs-error-boundary-admin@example.com",
    )
    admin_response = admin_client.get(
        "/api/v1/admin/jobs",
        params={"problems_only": True},
    )

    assert admin_response.status_code == 200
    admin_item = next(
        item
        for item in admin_response.json()["data"]["items"]
        if item["id"] == str(failed.id)
    )
    assert "last_error" not in admin_item
    assert admin_item["error_code"] == "job_failed"
    assert internal_error not in admin_response.text


def test_workspace_activity_feed_redacts_the_internal_job_error(page_service):
    """The Activity feed projects Job rows directly (ActivityService._jobs),
    bypassing job_read_projection's shared disclosure boundary. A viewer --
    the weakest workspace role that can read /activity at all -- must not
    receive the diagnostic text there either."""
    client = make_client(
        page_service, email="jobs-activity-viewer@example.com", role="viewer"
    )
    workspace_id = _workspace_id(client)
    failed = _enqueue(
        page_service,
        workspace_id,
        job_type="library_discovery",
        trigger_type="manual",
        idempotency_key="activity-error-boundary",
    )
    internal_error = "/srv/dtxwiki/private/converter.log token=secret-sentinel"
    _fail_terminally(page_service, failed.id, error=internal_error)

    response = client.get(f"/api/v1/workspaces/{workspace_id}/activity")

    assert response.status_code == 200
    assert internal_error not in response.text
    # Both halves of the sentinel separately: a leak of only the host path or
    # only the credential-shaped token must fail too.
    assert "secret-sentinel" not in response.text
    assert "/srv/dtxwiki/private" not in response.text
    item = next(
        entry
        for entry in response.json()["data"]["items"]
        if entry["id"] == str(failed.id)
    )
    # A stable code, not a rendered sentence: /activity is bilingual and the
    # client renders activity:jobs.errorCode.* in the reader's own language.
    assert item["body"] == "trigger=manual; progress=0%; error_code=job_failed"


def test_job_failure_code_replaces_last_error_on_both_job_surfaces(page_service):
    """Redaction alone left a workspace member with no reason at all. The code
    is derived only from Job's structured columns, so both workspace and
    server-admin surfaces return the same bounded failure shape."""
    workspace_client = make_client(page_service, email="jobs-disclosure@example.com")
    workspace_id = _workspace_id(workspace_client)
    failed = _enqueue(
        page_service,
        workspace_id,
        job_type="feedback_resolution",
        trigger_type="manual",
        idempotency_key="disclosure-failed",
    )
    internal_error = "/srv/dtxwiki/private/converter.log token=secret-sentinel"
    _fail_terminally(page_service, failed.id, error=internal_error)
    queued = _enqueue(
        page_service,
        workspace_id,
        job_type="library_discovery",
        trigger_type="manual",
        idempotency_key="disclosure-queued",
    )

    workspace_response = workspace_client.get(f"/api/v1/workspaces/{workspace_id}/jobs")

    assert workspace_response.status_code == 200
    workspace_items = {
        item["id"]: item for item in workspace_response.json()["data"]["items"]
    }
    assert "last_error" not in workspace_items[str(failed.id)]
    assert workspace_items[str(failed.id)]["error_code"] == "job_failed"
    # Never attempted, no error: nothing to disclose.
    assert workspace_items[str(queued.id)]["error_code"] is None

    admin_client = make_server_admin_client(
        page_service, email="jobs-disclosure-admin@example.com"
    )
    admin_response = admin_client.get("/api/v1/admin/jobs")

    assert admin_response.status_code == 200
    admin_item = next(
        item
        for item in admin_response.json()["data"]["items"]
        if item["id"] == str(failed.id)
    )
    assert "last_error" not in admin_item
    assert admin_item["error_code"] == "job_failed"
    assert internal_error not in admin_response.text


def test_workspace_jobs_and_activity_expose_the_same_stable_failure_code(page_service):
    """The two readers of Job.last_error must be one decision, so /jobs and
    /activity are asserted together here: a divergence between them is what
    let the Activity feed keep its own unredacted copy of the column."""
    client = make_client(page_service, email="jobs-shared-code@example.com")
    workspace_id = _workspace_id(client)
    failed = _enqueue(
        page_service,
        workspace_id,
        job_type="library_discovery",
        trigger_type="manual",
        idempotency_key="shared-failure-code",
    )
    internal_error = "/srv/dtxwiki/private/discovery.log token=sk-live-shared-sentinel"
    _fail_terminally(page_service, failed.id, error=internal_error)

    jobs = client.get(
        f"/api/v1/workspaces/{workspace_id}/jobs", params={"problems_only": True}
    )
    activity = client.get(
        f"/api/v1/workspaces/{workspace_id}/activity",
        params={"period": "all", "sub_tab": "live"},
    )

    assert jobs.status_code == 200
    assert activity.status_code == 200
    jobs_item = next(
        item for item in jobs.json()["data"]["items"] if item["id"] == str(failed.id)
    )
    activity_item = next(
        item
        for item in activity.json()["data"]["items"]
        if item["id"] == str(failed.id)
    )
    assert jobs_item.get("error_code") == "job_failed"
    assert "last_error" not in jobs_item
    assert "error_code=job_failed" in activity_item["body"]
    for response in (jobs, activity):
        assert "sk-live-shared-sentinel" not in response.text
        assert "/srv/dtxwiki/private" not in response.text


def test_list_workspace_jobs_rejects_invalid_status(page_service):
    client = make_client(page_service)
    workspace_id = _workspace_id(client)

    response = client.get(
        f"/api/v1/workspaces/{workspace_id}/jobs", params={"status": "bogus"}
    )

    assert response.status_code == 422


def test_workspace_job_list_cursor_stops_at_the_offset_cap(page_service, monkeypatch):
    """A capped offset cursor must terminate rather than decode back to the
    same page. list_workspace_jobs never fans out (it is already scoped to
    one session_for(workspace_id)), so it keeps the offset cursor + cap this
    pins -- unlike admin listing below, which moved to a seek cursor in
    Phase 6a (doc/design/workspace-database-separation-plan.md §4) and has no
    offset, so this cap does not apply to it any more (see the admin test)."""
    monkeypatch.setattr(job_read_service_module, "_MAX_JOB_OFFSET", 1)
    client = make_server_admin_client(page_service)
    workspace_id = _workspace_id(client)
    for index in range(3):
        _enqueue(
            page_service,
            workspace_id,
            job_type="library_discovery",
            trigger_type="manual",
            idempotency_key=f"cursor-cap-{index}",
        )

    workspace_first = client.get(
        f"/api/v1/workspaces/{workspace_id}/jobs", params={"limit": 1}
    ).json()["data"]
    assert workspace_first["next_cursor"] is not None
    workspace_last = client.get(
        f"/api/v1/workspaces/{workspace_id}/jobs",
        params={"limit": 1, "cursor": workspace_first["next_cursor"]},
    ).json()["data"]
    assert workspace_last["next_cursor"] is None


def test_admin_job_list_seek_cursor_pages_through_every_job_with_no_cap(
    page_service, monkeypatch
):
    """Phase 6a (doc/design/workspace-database-separation-plan.md §4): admin
    listing's cursor changed from an offset into one query to a (created_at,
    id) seek key re-applied per shard. _MAX_JOB_OFFSET was an offset-pagination
    defense (an attacker-supplied cursor cannot force an arbitrarily large
    OFFSET scan) that has nothing to bind to on a seek cursor -- patching it
    here must be inert, not truncate a real page early. With limit=1 and 3
    jobs, correct behavior is 3 pages (one job each), the third's next_cursor
    None -- not 2, which is what the old offset cap forced."""
    monkeypatch.setattr(job_read_service_module, "_MAX_JOB_OFFSET", 1)
    client = make_server_admin_client(page_service)
    workspace_id = _workspace_id(client)
    enqueued_ids = {
        str(
            _enqueue(
                page_service,
                workspace_id,
                job_type="library_discovery",
                trigger_type="manual",
                idempotency_key=f"seek-cap-{index}",
            ).id
        )
        for index in range(3)
    }

    seen_ids: list[str] = []
    cursor = None
    for _ in range(3):
        page = client.get(
            "/api/v1/admin/jobs",
            params={"limit": 1, **({"cursor": cursor} if cursor else {})},
        ).json()["data"]
        assert len(page["items"]) == 1
        seen_ids.append(page["items"][0]["id"])
        cursor = page["next_cursor"]
    assert cursor is None
    # Every job exactly once across the three pages -- not the old cap's
    # truncated-at-two-of-three behavior, and not a duplicate.
    assert set(seen_ids) == enqueued_ids
    assert len(seen_ids) == len(set(seen_ids))


def test_admin_job_list_tolerates_a_pre_phase_6a_cursor(page_service):
    """A cursor issued by the old offset-based admin listing (or any garbled
    value) decodes to no seek bound, restarting the listing from the top --
    same tolerant-cursor behavior _decode_job_cursor already had, carried
    over to _decode_fanout_cursor rather than 500ing or misreading an offset
    as a seek key."""
    client = make_server_admin_client(page_service)
    workspace_id = _workspace_id(client)
    _enqueue(
        page_service,
        workspace_id,
        job_type="library_discovery",
        trigger_type="manual",
        idempotency_key="pre-phase-6a-cursor",
    )

    stale_offset_cursor = base64.urlsafe_b64encode(b"job-offset:0").decode()
    response = client.get(
        "/api/v1/admin/jobs", params={"cursor": stale_offset_cursor}
    )

    assert response.status_code == 200, response.text
    assert len(response.json()["data"]["items"]) == 1


def test_admin_job_list_rejects_a_cursor_whose_prefix_is_not_its_own(page_service):
    """Isolate the seek cursor's prefix guard.

    The pre-Phase-6a test above cannot do this: "job-offset:0" carries no
    "|", so it is rejected by the separator check whether or not the prefix
    guard exists, and deleting the guard leaves that test green. This cursor
    is built to fail ONLY on the prefix -- same length as "job-seek:" so the
    body still slices at the right offset, and a body that parses cleanly
    into a (created_at, id) seek key.

    Without the guard the bogus key would be accepted as a real seek bound.
    Dated far in the past, that silently drops every job from a listing that
    should have restarted at the top, which is the failure mode worth pinning:
    not an error, a quietly short answer.
    """
    client = make_server_admin_client(page_service)
    workspace_id = _workspace_id(client)
    _enqueue(
        page_service,
        workspace_id,
        job_type="library_discovery",
        trigger_type="manual",
        idempotency_key="foreign-prefix-cursor",
    )

    foreign = f"job-peek:2000-01-01T00:00:00+00:00|{uuid4()}".encode()
    response = client.get(
        "/api/v1/admin/jobs",
        params={"cursor": base64.urlsafe_b64encode(foreign).decode()},
    )

    assert response.status_code == 200, response.text
    assert len(response.json()["data"]["items"]) == 1


def test_admin_jobs_endpoints_require_server_admin(page_service):
    client = make_client(page_service)
    workspace_id = _workspace_id(client)

    summary = client.get("/api/v1/admin/jobs/summary")
    listing = client.get("/api/v1/admin/jobs")
    retry = client.post(
        f"/api/v1/admin/jobs/{uuid4()}/retry",
        params={"workspace_id": workspace_id},
    )

    assert summary.status_code == 403
    assert listing.status_code == 403
    assert retry.status_code == 403


def test_admin_jobs_summary_and_list_reflect_queue_state(page_service):
    client = make_server_admin_client(page_service)
    workspace_id = _workspace_id(client)
    failed = _enqueue(
        page_service,
        workspace_id,
        job_type="library_discovery",
        trigger_type="manual",
        idempotency_key="admin-summary-failed",
    )
    _fail_terminally(page_service, failed.id)

    summary = client.get("/api/v1/admin/jobs/summary")
    assert summary.status_code == 200
    assert summary.json()["data"]["failed_count"] >= 1

    listing = client.get("/api/v1/admin/jobs", params={"problems_only": True})
    assert listing.status_code == 200
    items = listing.json()["data"]["items"]
    listed = next(item for item in items if item["id"] == str(failed.id))
    assert listed["workspace_slug"] == "default"
    assert listed["status"] == "failed"


def test_admin_retry_job_requeues_without_resetting_retry_count(page_service):
    client = make_server_admin_client(page_service)
    workspace_id = _workspace_id(client)
    job = _enqueue(
        page_service,
        workspace_id,
        job_type="library_discovery",
        trigger_type="manual",
        idempotency_key="admin-retry-success",
    )
    _fail_terminally(page_service, job.id)

    response = client.post(
        f"/api/v1/admin/jobs/{job.id}/retry",
        params={"workspace_id": workspace_id},
    )

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["status"] == "queued"
    assert data["retry_count"] == 1
    assert data["workspace_slug"] == "default"
    assert "last_error" not in data
    assert data["error_code"] == "job_retry_scheduled"


def test_admin_retry_job_rejects_domain_failure_state_type(page_service):
    client = make_server_admin_client(page_service)
    workspace_id = _workspace_id(client)
    job = _enqueue(
        page_service,
        workspace_id,
        job_type="extraction",
        trigger_type="manual",
        idempotency_key="admin-retry-unsupported",
    )
    _fail_terminally(page_service, job.id)

    response = client.post(
        f"/api/v1/admin/jobs/{job.id}/retry",
        params={"workspace_id": workspace_id},
    )

    assert response.status_code == 422
    assert response.json()["error_code"] == "job_retry_unsupported_type"


def test_admin_retry_job_not_found_or_not_failed(page_service):
    client = make_server_admin_client(page_service)
    workspace_id = _workspace_id(client)
    queued = _enqueue(
        page_service,
        workspace_id,
        job_type="library_discovery",
        trigger_type="manual",
        idempotency_key="admin-retry-not-failed",
    )

    still_queued = client.post(
        f"/api/v1/admin/jobs/{queued.id}/retry",
        params={"workspace_id": workspace_id},
    )
    missing = client.post(
        f"/api/v1/admin/jobs/{uuid4()}/retry",
        params={"workspace_id": workspace_id},
    )

    assert still_queued.status_code == 404
    assert still_queued.json()["error_code"] == "job_not_found"
    assert missing.status_code == 404


def test_admin_retry_job_with_a_workspace_id_that_does_not_own_it_is_not_found(
    page_service,
):
    """Epic-2 Phase 6c: JobQueueService.manual_retry's workspace_id guard is
    load-bearing on its own, not just a side effect of session_for routing to
    a different physical shard -- this fixture is single-DB (page_service),
    so a wrong workspace_id would land in the exact same database the real
    job lives in. Sabotage check: if manual_retry's `job.workspace_id !=
    workspace_id` comparison were removed, this job WOULD be found (it is a
    real row, currently "failed", type is generically retryable) and this
    test would catch that by finding it retried anyway.
    """
    client = make_server_admin_client(page_service)
    workspace_id = _workspace_id(client)
    job = _enqueue(
        page_service,
        workspace_id,
        job_type="library_discovery",
        trigger_type="manual",
        idempotency_key="admin-retry-wrong-workspace",
    )
    _fail_terminally(page_service, job.id)
    other_workspace_id = uuid4()
    assert str(other_workspace_id) != workspace_id

    response = client.post(
        f"/api/v1/admin/jobs/{job.id}/retry",
        params={"workspace_id": str(other_workspace_id)},
    )

    assert response.status_code == 404
    assert response.json()["error_code"] == "job_not_found"
    # Untouched: read the row back directly rather than through this same
    # endpoint's own success path, which would only prove the endpoint
    # renders whatever manual_retry returned -- not that the underlying row
    # was left alone.
    with page_service.session_factory() as verify_session:
        stored = verify_session.get(Job, job.id)
        assert stored.status == "failed"
        assert stored.retry_count == 1


def test_admin_retry_wrong_workspace_does_not_leak_an_unretryable_job_type(
    page_service,
):
    """The workspace guard has to stop the read, not just the write.

    manual_retry's UPDATE already carries `Job.workspace_id == workspace_id`,
    so a wrong workspace can never actually requeue a row -- which means the
    sibling test above passes with the READ-side comparison deleted, and does
    not isolate it. What the read-side guard alone protects is the branch
    between the two: a job type in JOB_TYPES_WITH_DOMAIN_FAILURE_STATE raises
    before the UPDATE is ever reached. Without the comparison, naming someone
    else's workspace turns that into a 422 that names the job's type, so a
    caller learns both that the id exists and what kind of work it is -- from
    an endpoint whose answer for an unknown id is a bare 404.

    This is single-DB on purpose: with a real shard behind each workspace the
    row would be unreachable anyway, and the guard would look redundant.
    """
    client = make_server_admin_client(page_service)
    workspace_id = _workspace_id(client)
    unretryable_type = sorted(JOB_TYPES_WITH_DOMAIN_FAILURE_STATE)[0]
    job = _enqueue(
        page_service,
        workspace_id,
        job_type=unretryable_type,
        trigger_type="manual",
        idempotency_key="admin-retry-wrong-workspace-leak",
    )
    _fail_terminally(page_service, job.id)
    other_workspace_id = uuid4()

    response = client.post(
        f"/api/v1/admin/jobs/{job.id}/retry",
        params={"workspace_id": str(other_workspace_id)},
    )

    assert response.status_code == 404, response.text
    body = response.json()
    assert body["error_code"] == "job_not_found"
    assert unretryable_type not in response.text
    assert body["error_code"] != "job_retry_unsupported_type"


def test_workspace_jobs_are_not_readable_by_a_non_member(page_service):
    """The listing is workspace-scoped, so membership -- not authentication --
    has to be what admits a reader. A signed-in user with no membership in the
    target workspace must not learn that workspace's job state, including
    whether a job exists at all.
    """
    owner = make_client(page_service, email="jobs-owner@example.com")
    owner_workspace = _workspace_id(owner)
    job = _enqueue(
        page_service,
        owner_workspace,
        job_type="library_conversion",
        trigger_type="manual",
        target_ref={"probe": "isolation"},
    )
    _fail_terminally(page_service, job.id, error="owner-only failure detail")

    # A second authenticated identity that was never granted membership in the
    # owner's workspace. make_client() would opt it INTO the seeded workspace,
    # which is exactly what must not happen here -- signup alone gives a user
    # only their own Personal workspace.
    outsider = TestClient(
        create_app(
            settings=Settings(
                database_url="sqlite+pysqlite:///:memory:",
                session_secret="test-session-secret",
            ),
            page_service=page_service,
        )
    )
    authenticate_test_client(outsider, email="jobs-outsider@example.com")
    assert _workspace_id(outsider) != owner_workspace

    response = outsider.get(f"/api/v1/workspaces/{owner_workspace}/jobs")
    assert response.status_code in {403, 404}, response.text
    assert "owner-only failure detail" not in response.text
    assert str(job.id) not in response.text

    # Two independent defences stand between the outsider and this data, and
    # the outer one masks the inner. Removing the endpoint's
    # WorkspaceViewerAccessDep still yields 404 "workspace not found" from the
    # routing layer, so a test that only checks the response cannot tell which
    # defence held -- and would keep passing if the membership check were
    # deleted. Assert the dependency is on the endpoint as well, so the inner
    # guard cannot be removed silently behind the outer one.
    signature = inspect.signature(read_api.list_workspace_jobs)
    assert any(
        parameter.annotation is WorkspaceViewerAccessDep
        for parameter in signature.parameters.values()
    ), "list_workspace_jobs must keep its own membership check"

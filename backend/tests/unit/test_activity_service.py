from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

from backend.policy.auth import ActorContext
from backend.schemas.activity import ActivityItemRead
from backend.services.activity_service import (
    _MAX_ACTIVITY_OFFSET,
    ActivityService,
    _audit_target_visible,
    _encode_activity_cursor,
    activity_event_metadata,
    job_summary,
)
from backend.services.job_read_service import (
    PUBLIC_JOB_FAILURE_CODES,
    job_failure_disclosure,
    public_job_failure_code,
)
from backend.services.review_service import ReviewService


def test_context_activity_owner_recovery_requires_declared_colocation():
    # One SQL-boundary scenario, contrasting undeclared and declared topology:
    # an owner fallback can fail on a workspace shard with no Users table even
    # when every returned decision is public. Unified-DB integration fixtures
    # cannot expose that missing-table failure, so inspect the actual compiled
    # projection query here rather than adding another database/E2E fixture.
    from sqlalchemy.dialects import sqlite

    from backend.app.container import SINGLE_DATABASE_TOPOLOGY
    from backend.services.activity_projection import ContextActivityProjection

    class RecordingSession:
        def __init__(self):
            self.info = {}
            self.statements = []

        def scalars(self, statement):
            self.statements.append(statement)
            return []

        def last_sql(self):
            return str(self.statements[-1].compile(dialect=sqlite.dialect()))

    session = RecordingSession()
    workspace_id, principal = uuid4(), uuid4()
    actor = ActorContext(
        actor_kind="human",
        actor_id=str(principal),
        user_id=principal,
        workspace_id=workspace_id,
        workspace_role="owner",
        role="owner",
    )
    rows = [
        SimpleNamespace(
            target_kind="system",
            target_id=None,
            payload={"decision_id": str(uuid4())},
        )
    ]

    ContextActivityProjection(session, workspace_id, actor, rows)
    shard_sql = session.last_sql()
    assert "FROM users" not in shard_sql
    assert "review_decision.audience_user_id =" in shard_sql
    assert "review_decision.audience_kind IS NULL" in shard_sql

    session.info["database_provider"] = SimpleNamespace(
        storage_topology=SINGLE_DATABASE_TOPOLOGY
    )
    ContextActivityProjection(session, workspace_id, actor, rows)
    assert "FROM users" in session.last_sql()


def test_activity_event_metadata_keys_known_actions_and_leaves_unknown_unkeyed():
    keyed = SimpleNamespace(
        action="tree.node.rename",
        payload={"previous_title": "Old", "title": "New", "ignored": "value"},
    )
    unkeyed = SimpleNamespace(action="legacy.event", payload={"title": "Legacy"})

    assert activity_event_metadata(keyed, visible=True) == (
        "tree.node.rename",
        {"previous_title": "Old", "title": "New"},
    )
    assert activity_event_metadata(unkeyed, visible=True) == (None, {})
    assert activity_event_metadata(keyed, visible=False) == (None, {})


def test_activity_event_metadata_keys_join_request_actions():
    # UI-08: without an _ACTIVITY_EVENT_ARGUMENTS entry, these three actions
    # fell through to the unkeyed branch and the client rendered the raw
    # audit code humanized into "Workspace.join request.requested" for every
    # row, regardless of requester.
    requested = SimpleNamespace(
        action="workspace.join_request.requested",
        payload={"attempt_no": 1, "target_title": "Riley Requester"},
    )
    approved = SimpleNamespace(
        action="workspace.join_request.approved",
        payload={"attempt_no": 2, "granted_role": "viewer"},
    )
    rejected = SimpleNamespace(
        action="workspace.join_request.rejected",
        payload={"attempt_no": 1},
    )

    assert activity_event_metadata(requested, visible=True) == (
        "workspace.join_request.requested",
        {"attempt_no": "1"},
    )
    assert activity_event_metadata(approved, visible=True) == (
        "workspace.join_request.approved",
        {"attempt_no": "2", "granted_role": "viewer"},
    )
    assert activity_event_metadata(rejected, visible=True) == (
        "workspace.join_request.rejected",
        {"attempt_no": "1"},
    )


def test_audit_target_visible_gates_join_requests_to_owners_only():
    # UI-08 part 2: the requester's real name now rides the audit payload
    # (target_title), so this kind must be owner-only, not the "any
    # actor" default every other target-less/insensitive kind gets.
    event = SimpleNamespace(target_id=uuid4(), target_kind="workspace_join_request")
    owner = ActorContext(actor_kind="human", actor_id="u1", role="owner")
    viewer = ActorContext(actor_kind="human", actor_id="u2", role="viewer")

    assert _audit_target_visible(session=None, event=event, actor=owner) is True
    assert _audit_target_visible(session=None, event=event, actor=viewer) is False
    assert _audit_target_visible(session=None, event=event, actor=None) is False


def test_activity_cursor_stops_at_max_offset_boundary(monkeypatch):
    service = ActivityService(review_service=ReviewService())
    base = datetime.now(UTC)
    items = [
        ActivityItemRead(
            id=uuid4(),
            lane="history",
            event_key=None,
            event_args={},
            title=f"history {index}",
            body="context_page.updated",
            actor=None,
            target=None,
            created_at=base - timedelta(seconds=index),
            review_item_id=None,
            affordances=["open"],
        )
        for index in range(_MAX_ACTIVITY_OFFSET + 3)
    ]

    monkeypatch.setattr(service, "_pending_reviews", lambda **_kwargs: [])
    monkeypatch.setattr(service, "_jobs", lambda **_kwargs: [])
    monkeypatch.setattr(service, "_mcp_traces", lambda **_kwargs: [])
    monkeypatch.setattr(service, "_audit_history", lambda **_kwargs: items)

    result = service.list_activity(
        session=object(),
        workspace_id=uuid4(),
        period="all",
        lane="history",
        limit=2,
        cursor=_encode_activity_cursor(_MAX_ACTIVITY_OFFSET),
        actor_role="owner",
    )

    assert len(result.items) == 2
    assert result.next_cursor is None


def test_list_activity_review_decision_kind_keeps_only_review_decisions(monkeypatch):
    # RD-3: kind="review_decision" surfaces only review.decision audit rows
    # (the projection sets body=action), dropping other workspace/system rows.
    service = ActivityService(review_service=ReviewService())
    base = datetime.now(UTC)
    items = [
        ActivityItemRead(
            id=uuid4(),
            lane="history",
            event_key=None,
            event_args={},
            title="Approved a page",
            body="review.decision",
            actor=None,
            target=None,
            created_at=base,
            review_item_id=None,
            affordances=["open"],
        ),
        ActivityItemRead(
            id=uuid4(),
            lane="history",
            event_key=None,
            event_args={},
            title="Edited a page",
            body="context_page.updated",
            actor=None,
            target=None,
            created_at=base - timedelta(seconds=1),
            review_item_id=None,
            affordances=["open"],
        ),
        ActivityItemRead(
            id=uuid4(),
            lane="history",
            event_key=None,
            event_args={},
            title="Signed in",
            body="auth.login",
            actor=None,
            target=None,
            created_at=base - timedelta(seconds=2),
            review_item_id=None,
            affordances=["open"],
        ),
    ]

    monkeypatch.setattr(service, "_pending_reviews", lambda **_kwargs: [])
    monkeypatch.setattr(service, "_jobs", lambda **_kwargs: [])
    monkeypatch.setattr(service, "_mcp_traces", lambda **_kwargs: [])
    monkeypatch.setattr(service, "_audit_history", lambda **_kwargs: items)

    result = service.list_activity(
        session=object(),
        workspace_id=uuid4(),
        period="all",
        kind="review_decision",
        limit=50,
        actor_role="owner",
    )

    assert [item.body for item in result.items] == ["review.decision"]


# Arbitrary worker ``str(exc)`` output: a host path plus a credential-shaped
# token, so a leak of either half fails loudly instead of vacuously.
_WORKER_ERROR_SENTINEL = "/srv/dtxwiki/private/roadmap.md token=sk-live-unit-sentinel"


def test_job_summary_reports_a_stable_failure_code_not_the_raw_worker_error():
    # ActivityService._jobs projects the Job row straight into the feed, so a
    # handler's str(exc) text (internal paths, provider responses, secrets)
    # reached every workspace viewer here even though job_read_projection
    # redacts the same column on GET /workspaces/{id}/jobs. The body carries a
    # closed-vocabulary code rather than a rendered sentence, because the feed
    # is bilingual and the client owns the copy.
    failed = SimpleNamespace(
        status="failed",
        trigger_type="manual",
        progress=0,
        stage=None,
        retry_count=1,
        last_error=_WORKER_ERROR_SENTINEL,
    )
    retrying = SimpleNamespace(
        status="queued",
        trigger_type="manual",
        progress=0,
        stage=None,
        retry_count=1,
        last_error=_WORKER_ERROR_SENTINEL,
    )
    clean = SimpleNamespace(
        status="completed",
        trigger_type="scheduled",
        progress=100,
        stage=None,
        retry_count=0,
        last_error=None,
    )

    failed_summary = job_summary(failed)
    retrying_summary = job_summary(retrying)
    clean_summary = job_summary(clean)

    assert failed_summary == "trigger=manual; progress=0%; error_code=job_failed"
    # An attempt that failed and is waiting to run again is the state a
    # workspace member most needs named; silence here reads as "nothing wrong".
    assert retrying_summary == (
        "trigger=manual; progress=0%; error_code=job_retry_scheduled"
    )
    assert clean_summary == "trigger=scheduled; progress=100%"
    for summary in (failed_summary, retrying_summary):
        assert "sk-live-unit-sentinel" not in summary
        assert "/srv/dtxwiki/private" not in summary


def test_public_job_failure_code_is_a_closed_vocabulary_derived_from_status():
    for status, expected in (
        ("failed", "job_failed"),
        ("queued", "job_retry_scheduled"),
        ("running", "job_retry_scheduled"),
        # A succeeded job carrying a stale error from an earlier attempt is
        # not a live problem.
        ("completed", None),
    ):
        job = SimpleNamespace(
            status=status, retry_count=1, last_error=_WORKER_ERROR_SENTINEL
        )
        code = public_job_failure_code(job)

        assert code == expected
        assert code is None or code in PUBLIC_JOB_FAILURE_CODES

    # last_error is read for its truthiness only, never for its text.
    assert (
        public_job_failure_code(
            SimpleNamespace(status="failed", retry_count=0, last_error=None)
        )
        is None
    )


def test_job_failure_disclosure_never_returns_the_raw_error():
    failed = SimpleNamespace(
        status="failed", retry_count=1, last_error=_WORKER_ERROR_SENTINEL
    )

    disclosure = job_failure_disclosure(failed)
    assert disclosure == {"error_code": "job_failed"}
    assert "last_error" not in disclosure

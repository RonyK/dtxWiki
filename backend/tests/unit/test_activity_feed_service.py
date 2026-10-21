from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from backend.policy.auth import ActorContext
from backend.schemas.activity import ActivityActor, ActivityFeedEvent, ActivityItemRead
from backend.services.activity_feed_service import (
    ActivityFeedService,
    InvalidActivityTokenError,
    _VisibleEvent,
)
from backend.services.activity_service import ActivityService
from backend.services.review_service import ReviewService

BASE = datetime(2026, 10, 20, 12, tzinfo=UTC)
KEY = ("client", "session", "context_page", "page", "read", "version", "whole")


def row(index, *, key=KEY, source="mcp", operation="read", at=None, lane="history"):
    return _VisibleEvent(
        ActivityFeedEvent(
            id=UUID(int=index + 1),
            lane=lane,
            event_key=None,
            event_args={},
            title="Activity",
            body=None,
            actor=ActivityActor(actor_kind="mcp_client", display_name="Client"),
            target=None,
            created_at=at or BASE - timedelta(seconds=index),
            review_item_id=None,
            affordances=["open"],
            operation=operation,
            result_status="succeeded",
            tool_name="read",
        ),
        source,
        key,
    )


@pytest.fixture
def service():
    return ActivityFeedService(
        ActivityService(ReviewService()), cursor_secret="test-secret"
    )


def entries(service, rows, *, kind="all", subtype="all", group=True):
    return list(service._entries(iter(rows), kind, subtype, None, group))


def test_grouping_matches_identity_and_total_span_not_adjacent_gaps(service):
    rows = [row(i, at=BASE - timedelta(seconds=i * 120)) for i in range(4)]
    assert [len(e.members) for e in entries(service, rows)] == [3, 1]
    assert [
        len(e.members) for e in entries(service, [row(0), row(1, key=KEY + ("other",))])
    ] == [1, 1]


def test_grouping_maximum50_and_date_boundary(service):
    assert [len(e.members) for e in entries(service, [row(i) for i in range(51)])] == [
        50,
        1,
    ]
    midnight = datetime(2026, 10, 20, 0, 0, 1, tzinfo=UTC)
    assert [
        len(e.members)
        for e in entries(
            service, [row(0, at=midnight), row(1, at=midnight - timedelta(seconds=2))]
        )
    ] == [1, 1]


@pytest.mark.parametrize("operation", ["search", "edit", "tool"])
def test_visible_filtered_boundary_prevents_two_reads_merging(service, operation):
    rows = [row(0), row(1, key=None, operation=operation), row(2)]
    assert [
        len(e.members) for e in entries(service, rows, kind="mcp", subtype="read")
    ] == [1, 1]


def test_ungrouped_mode_has_same_complete_ledger(service):
    rows = [row(i) for i in range(8)]
    grouped = entries(service, rows)
    raw = entries(service, rows, group=False)
    assert [r.event.id for e in grouped for r in e.members] == [
        r.event.id for e in raw for r in e.members
    ]
    assert len(grouped) == 1 and len(raw) == 8


def test_cursor_binds_scope_filter_and_expiry(service, monkeypatch):
    actor = ActorContext(actor_kind="human", actor_id="owner", user_id=uuid4())
    workspace_id = uuid4()
    rows = [row(i, key=None) for i in range(4)]

    def stream(*_args, after=None, **_kwargs):
        return iter([item for item in rows if after is None or item.key < after])

    monkeypatch.setattr(service, "_visible_stream", stream)
    monkeypatch.setattr(service, "_attention_count", lambda *_a: 3)
    result = service.list_feed(
        None,
        workspace_id,
        actor=actor,
        actor_role="owner",
        period="all",
        lane="history",
        limit=2,
    )
    assert result.returned_entry_count == result.represented_event_count == 2
    assert result.unread_count == 3
    assert result.next_cursor and result.has_more
    second = service.list_feed(
        None,
        workspace_id,
        actor=actor,
        actor_role="owner",
        period="all",
        lane="history",
        limit=2,
        cursor=result.next_cursor,
    )
    assert [e.event.id for e in result.entries + second.entries] == [
        item.event.id for item in rows
    ]
    assert not second.has_more and second.next_cursor is None
    for changes in [
        dict(workspace_id=uuid4()),
        dict(group_reads=False),
        dict(kind="workspace"),
        dict(actor=ActorContext(actor_kind="human", actor_id="other")),
    ]:
        arguments = dict(
            workspace_id=workspace_id,
            actor=actor,
            actor_role="owner",
            period="all",
            lane="history",
            limit=2,
            cursor=result.next_cursor,
        )
        arguments.update(changes)
        with pytest.raises(InvalidActivityTokenError):
            service.list_feed(None, **arguments)
    payload = service._decode(result.next_cursor, "cursor", None, datetime.now(UTC))
    payload["expires"] = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    with pytest.raises(InvalidActivityTokenError):
        service.list_feed(
            None,
            workspace_id,
            actor=actor,
            actor_role="owner",
            period="all",
            lane="history",
            cursor=service._encode(payload),
        )
    with pytest.raises(InvalidActivityTokenError):
        service.list_feed(
            None,
            workspace_id,
            actor=actor,
            actor_role="owner",
            period="all",
            lane="history",
            cursor=result.next_cursor + "x",
        )
    # Another app node's clock may run slightly ahead of this one.
    now = datetime.now(UTC)
    payload = service._decode(result.next_cursor, "cursor", None, now)
    payload["snapshot"] = (now + timedelta(seconds=30)).isoformat()
    assert service._decode(service._encode(payload), "cursor", None, now)
    payload["snapshot"] = (now + timedelta(minutes=5)).isoformat()
    with pytest.raises(InvalidActivityTokenError):
        service._decode(service._encode(payload), "cursor", None, now)


def test_deferred_actor_is_not_copied_and_count_is_not_affected_by_expansion(service):
    original = row(0).event
    event = service._event(original, "audit")
    assert event.actor is original.actor
    original.actor.display_name = "Resolved identity"
    assert event.actor.display_name == "Resolved identity"


def test_overview_reserves_real_review_only_and_lane_metadata_is_scoped(
    service, monkeypatch
):
    actor = ActorContext(actor_kind="human", actor_id="owner")
    review = row(100, key=None, source="review", lane="pending").event
    review.review_item_id = uuid4()
    advisory = row(0, key=None, lane="advisory")
    history = row(1, key=None)
    failed_job = row(2, key=None, source="job", lane="pending")
    monkeypatch.setattr(service.activity, "_pending_reviews", lambda **_kw: [review])
    monkeypatch.setattr(
        service,
        "_visible_stream",
        lambda *_a, **_kw: iter([advisory, history, failed_job]),
    )
    monkeypatch.setattr(service, "_attention_count", lambda *_a: 2)
    result = service.list_feed(
        None, uuid4(), actor=actor, actor_role="owner", period="all", limit=2
    )
    assert {e.event.id for e in result.entries} == {review.id, advisory.event.id}
    assert result.has_more and result.next_cursor is None
    assert result.lanes["history"].has_more is True
    assert result.lanes["advisory"].has_more is False
    assert result.lanes["pending"].represented_event_count == 1


def test_invalid_subtype_cannot_silently_broaden_query(service):
    with pytest.raises(InvalidActivityTokenError):
        service.list_feed(
            None,
            uuid4(),
            actor=ActorContext(actor_kind="human", actor_id="a"),
            actor_role="owner",
            kind="system",
            subtype="read",
        )


def test_eight_originals_five_entries_counts_remain_unchanged_after_details(
    service, monkeypatch
):
    actor = ActorContext(actor_kind="human", actor_id="reader")
    workspace_id = uuid4()
    rows = [row(i) for i in range(4)] + [
        row(i, key=None, operation="edit") for i in range(4, 8)
    ]
    monkeypatch.setattr(service, "_visible_stream", lambda *_a, **_kw: iter(rows))
    monkeypatch.setattr(service, "_attention_count", lambda *_a: 7)
    grouped = service.list_feed(
        None,
        workspace_id,
        actor=actor,
        actor_role="owner",
        period="all",
        kind="mcp",
        lane="history",
    )
    raw = service.list_feed(
        None,
        workspace_id,
        actor=actor,
        actor_role="owner",
        period="all",
        kind="mcp",
        lane="history",
        group_reads=False,
    )
    assert (grouped.returned_entry_count, grouped.represented_event_count) == (5, 8)
    assert (raw.returned_entry_count, raw.represented_event_count) == (8, 8)
    assert grouped.unread_count == raw.unread_count == 7
    assert grouped.lanes["history"].represented_event_count == 8
    group = grouped.entries[0]
    monkeypatch.setattr(
        service,
        "_hydrate",
        lambda *_a, **_kw: {("mcp", item.event.id): item for item in rows[:4]},
    )
    details = service.read_group(
        None, workspace_id, actor=actor, actor_role="owner", token=group.group_token
    )
    assert details.event_count == 4
    assert (grouped.returned_entry_count, grouped.represented_event_count) == (5, 8)


def test_same_snapshot_and_member_set_produce_stable_opaque_group_id(service):
    actor = ActorContext(actor_kind="human", actor_id="reader")
    fingerprint = service._fingerprint(
        uuid4(), actor, "all", "mcp", "all", "history", True
    )
    entry = entries(service, [row(0), row(1)])[0]
    snapshot = datetime.now(UTC)
    first = service._serialize_entry(entry, fingerprint, snapshot, None, None)
    second = service._serialize_entry(entry, fingerprint, snapshot, None, None)
    assert first.id == second.id and first.group_token == second.group_token
    assert str(entry.members[0].event.id) not in first.id


@pytest.mark.parametrize(
    ("action", "operation", "result"),
    [
        ("page.autosave.draft", "edit", None),
        ("page.patch_context_edit", "edit", None),
        ("page.begin_context_edit", "log", None),
        ("page.discard_context_edit", "delete", None),
        ("page.submit_context_edit_for_review", "review", None),
        ("page.restore_version", "restore", None),
        ("context_page.draft.update", "edit", None),
        ("library.item.register", "create", None),
        ("library.item.version.create", "create", None),
        ("agent_page.draft.create", "create", None),
        ("agent_page.draft.update", "edit", None),
        ("agent_page.activated", "publish", None),
        ("library.extraction.failed", "log", "failed"),
        ("library.conversion.failed", "log", "failed"),
        ("auth.password.change.failed", "log", "failed"),
        ("auth.reauthentication.failed", "log", "failed"),
        ("oauth.permission.denied", "log", "rejected_by_policy"),
        ("mcp.client.runtime.denied", "log", "rejected_by_policy"),
        ("library.upload.accepted", "create", "accepted"),
        ("workspace.join_request.rejected", "review", None),
        ("context_ingress.proposal.reject", "review", None),
        ("unknown.future.create", "log", None),
        ("unknown.future.draft", "log", None),
    ],
)
def test_real_audit_vocabulary_preserves_operations_and_failure_results(
    service, action, operation, result
):
    original = row(0).event
    values = {name: getattr(original, name) for name in ActivityItemRead.model_fields}
    values["body"] = action
    event = service._event(ActivityItemRead(**values), "audit")
    assert (event.operation, event.result_status) == (operation, result)

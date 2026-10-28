from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select

from backend.models.inbox import WorkspaceInboxEntry
from backend.policy.auth import ActorContext
from backend.services.inbox import (
    STORED,
    ComputedScan,
    ComputedSource,
    InboxDisplay,
    InboxKind,
    InboxListener,
    InboxRegistry,
    InboxService,
    InboxSnapshot,
    InboxViewer,
    WorkspaceInbox,
)
from backend.services.read_errors import ReadNotFoundError


def test_inbox_core_marks_only_the_seen_revision_for_its_own_recipient(
    session_factory,
):
    """One scenario through the policy-free core with test-only kinds."""

    workspace_id, alice, bob = uuid4(), uuid4(), uuid4()
    scope = WorkspaceInbox(workspace_id)
    start = datetime(2026, 10, 28, tzinfo=UTC)
    status = {"queue": InboxSnapshot("queue", "r1", start)}

    def resolve(_session, _viewer, snapshots):
        return {
            snapshot.item_key: InboxDisplay(
                title=snapshot.item_key, href=f"/workspaces/{workspace_id}/review"
            )
            for snapshot in snapshots
            if not (snapshot.ref or {}).get("hidden")
        }

    note = InboxKind("test.note", "workspace", STORED, resolve)
    retired = InboxKind("test.retired", "workspace", STORED, resolve)
    computed = InboxKind(
        "test.status",
        "workspace",
        ComputedSource(
            scan=lambda _s, _v, _limit: ComputedScan(
                list(status.values()), len(status)
            ),
            get=lambda _s, _v, key: status.get(key),
        ),
        resolve,
    )
    delivered = []

    class RecordingChannel(InboxListener):
        def on_put(self, _session, put_scope, kind, recipient_ids, snapshot):
            delivered.append((put_scope, kind, recipient_ids, snapshot.revision))

    inbox = InboxService(InboxRegistry((note, computed)), (RecordingChannel(),))

    def viewer(user_id):
        actor = ActorContext("human", str(user_id), user_id=user_id)
        return InboxViewer(actor, user_id, scope, role="viewer")

    def put(key, revision, minutes, *, ref=None, service=inbox, kind="test.note"):
        with session_factory.begin() as session:
            service.put(
                session,
                scope=scope,
                recipient_ids=[alice],
                kind=kind,
                item_key=key,
                revision=revision,
                event_at=start + timedelta(minutes=minutes),
                ref=ref,
            )

    def page(user_id=alice, **options):
        with session_factory() as session:
            return inbox.list_items(session, viewer(user_id), **options)

    def mark(key, revision, user_id=alice, kind="test.note", **marks):
        with session_factory.begin() as session:
            return inbox.mark(
                session,
                viewer(user_id),
                kind=kind,
                item_key=key,
                seen_revision=revision,
                **marks,
            )

    put("note", "v1", 2)
    put("hidden", "v1", 3, ref={"hidden": True})
    # A kind whose code was removed still lists, without content or link.
    retired_inbox = InboxService(InboxRegistry((retired,)))
    put("old", "v1", 1, service=retired_inbox, kind="test.retired")

    first = page(limit=1)
    assert [(i.item_key, i.unread) for i in first.items] == [("note", True)]
    assert first.items[0].display.href == f"/workspaces/{workspace_id}/review"
    assert [(i.kind, i.revision, i.unread) for i in first.status_items] == [
        ("test.status", "r1", True)
    ]
    second = page(limit=1, cursor=first.next_cursor)
    assert [(i.item_key, i.display) for i in second.items] == [("old", None)]
    assert second.status_items == [] and second.next_cursor is None

    # Another recipient sees and marks none of these.
    assert page(user_id=bob).items == []
    with pytest.raises(ReadNotFoundError):
        mark("note", "v1", user_id=bob, read=True)
    with pytest.raises(ReadNotFoundError):
        mark("hidden", "v1", read=True)

    # put v2 -> read(v2) -> a late read(v1) must not consume v2 again.
    put("note", "v2", 4)
    # A delivery channel sees each write once, with its recipients.
    assert delivered[-1] == (scope, "test.note", (alice,), "v2")
    assert len(delivered) == 3
    assert mark("note", "v2", read=True).applied
    late = mark("note", "v1", read=True)
    assert not late.applied and (late.item.revision, late.item.unread) == ("v2", False)
    assert [i.item_key for i in page(state="unread").items] == ["old"]

    # Archive holds across a new revision unless the kind reopens it.
    assert mark("note", "v2", archived=True).applied
    put("note", "v3", 5)
    archived = page(state="archived").items
    assert [(i.item_key, i.revision, i.unread) for i in archived] == [
        ("note", "v3", True)
    ]
    assert "note" not in [i.item_key for i in page().items]

    # Computed items keep marks in mark rows, which never list as items.
    assert mark("queue", "r1", kind="test.status", read=True).applied
    assert page(state="unread").status_items == []
    status["queue"] = InboxSnapshot("queue", "r2", start)
    assert [i.revision for i in page(state="unread").status_items] == ["r2"]
    with pytest.raises(ReadNotFoundError):
        mark("missing", "r1", kind="test.status", read=True)
    with session_factory() as session:
        rows = session.scalars(
            select(WorkspaceInboxEntry).where(WorkspaceInboxEntry.mode == "mark")
        ).all()
    assert [(row.item_key, row.read_revision) for row in rows] == [("queue", "r1")]
    assert all(i.kind != "test.status" for i in page(state="all").items)

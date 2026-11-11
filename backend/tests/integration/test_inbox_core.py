from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import event, select

from backend.models.inbox import WorkspaceInboxEntry
from backend.policy.auth import ActorContext
from backend.services.inbox import (
    STORED,
    ComputedScan,
    ComputedSource,
    InboxDisplay,
    InboxKind,
    InboxListener,
    InboxListingConfig,
    InboxRegistry,
    InboxService,
    InboxSnapshot,
    InboxViewer,
    WorkspaceInbox,
)
from backend.services.inbox.recipients import retire_workspace_recipient
from backend.services.read_errors import ReadNotFoundError, ReadValidationError


def test_inbox_core_marks_only_the_seen_revision_for_its_own_recipient(
    session_factory,
):
    """Core lifecycle and distinct pagination regressions with test-only kinds."""

    workspace_id, alice, bob = uuid4(), uuid4(), uuid4()
    scope = WorkspaceInbox(workspace_id)
    start = datetime(2026, 10, 28, tzinfo=UTC)
    status = {"queue": InboxSnapshot("queue", "r1", start)}
    secondary = {}
    lookups = []

    def scan(found, kind, limit, after):
        ordered = sorted(
            found.values(),
            key=lambda item: (item.event_at, item.item_key),
            reverse=True,
        )
        if after is not None:
            ordered = [
                item for item in ordered if (item.event_at, kind, item.item_key) < after
            ]
        return ComputedScan(ordered[:limit], len(ordered) > limit)

    def lookup(_session, _viewer, keys):
        lookups.append(tuple(keys))
        return {key: status[key] for key in keys if key in status}

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
            scan=lambda _s, _v, limit, after: scan(status, "test.status", limit, after),
            lookup=lookup,
        ),
        resolve,
        reopen_on_new_revision=True,
    )
    other_computed = InboxKind(
        "test.secondary",
        "workspace",
        ComputedSource(
            scan=lambda _s, _v, limit, after: scan(
                secondary, "test.secondary", limit, after
            ),
            lookup=lambda _s, _v, keys: {
                key: secondary[key] for key in keys if key in secondary
            },
        ),
        resolve,
        reopen_on_new_revision=True,
    )
    delivered = []

    class RecordingChannel(InboxListener):
        def on_put(self, _session, put_scope, kind, recipient_ids, snapshot):
            delivered.append((put_scope, kind, recipient_ids, snapshot.revision))

    inbox = InboxService(
        InboxRegistry((note, computed, other_computed)), (RecordingChannel(),)
    )

    def viewer(user_id):
        actor = ActorContext("human", str(user_id), user_id=user_id)
        return InboxViewer(actor, user_id, scope, role="viewer")

    def put(
        key,
        revision,
        minutes,
        *,
        ref=None,
        service=inbox,
        kind="test.note",
        user_id=alice,
    ):
        with session_factory.begin() as session:
            service.put(
                session,
                scope=scope,
                recipient_ids=[user_id],
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
    assert first.status_items == []
    second = page(limit=1, cursor=first.next_cursor)
    assert [(i.item_key, i.display) for i in second.items] == [("old", None)]
    assert second.status_items == [] and second.next_cursor is not None
    third = page(limit=1, cursor=second.next_cursor)
    assert third.items == [] and third.next_cursor is None
    assert [(i.kind, i.revision, i.unread) for i in third.status_items] == [
        ("test.status", "r1", True)
    ]

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

    # Every kind supports the same archive/restore marks, independent of read.
    assert mark("queue", "r2", kind="test.status", archived=True).applied
    assert page().status_items == []
    assert page(state="unread").status_items == []
    queued = next(i for i in page(state="archived").items if i.kind == "test.status")
    assert (queued.item_key, queued.archived, queued.unread) == ("queue", True, True)
    assert not mark("queue", "r1", kind="test.status", archived=False).applied
    assert mark("queue", "r2", kind="test.status", archived=False).applied
    assert [i.item_key for i in page().status_items] == ["queue"]
    assert mark("queue", "r2", kind="test.status", archived=True).applied
    status["queue"] = InboxSnapshot("queue", "r3", start)
    assert [i.revision for i in page(state="unread").status_items] == ["r3"]
    assert all(i.kind != "test.status" for i in page(state="archived").items)

    # The twenty-item budget merges both types chronologically. Tied event
    # times across kinds have a stable boundary, and a recent stored message
    # appears among statuses instead of waiting for every status to finish.
    # Budget exception: the earlier lifecycle never spans a full mixed
    # twenty-item page or a tied boundary between computed kinds.
    # Keep real stored-row selection/hydration alongside computed candidates;
    # a pure merge test cannot catch their repository/service boundary errors.
    status.clear()
    status.update(
        {
            f"p{n:02}": InboxSnapshot(
                f"p{n:02}",
                "r1",
                start + timedelta(minutes=3 if n == 1 else -19 if n == 10 else -2 * n),
            )
            for n in range(1, 23)
        }
    )
    secondary.update(
        {
            f"s{n:02}": InboxSnapshot(
                f"s{n:02}",
                "r1",
                start + timedelta(minutes=3 if n == 1 else 1 - 2 * n),
            )
            for n in range(1, 18)
        }
    )

    def chronological_keys(result):
        return [
            (item.kind, item.item_key)
            for item in sorted(
                result.status_items + result.items,
                key=lambda item: (item.event_at, item.kind, item.item_key),
                reverse=True,
            )
        ]

    first_twenty = page()
    assert len(first_twenty.status_items) + len(first_twenty.items) == 20
    assert chronological_keys(first_twenty)[:3] == [
        ("test.status", "p01"),
        ("test.secondary", "s01"),
        ("test.retired", "old"),
    ]
    assert chronological_keys(first_twenty)[-1] == ("test.status", "p10")
    assert [
        i.item_key for i in first_twenty.status_items if i.kind == "test.status"
    ] == [f"p{n:02}" for n in range(1, 11)]
    assert [
        i.item_key for i in first_twenty.status_items if i.kind == "test.secondary"
    ] == [f"s{n:02}" for n in range(1, 10)]
    assert first_twenty.next_cursor is not None
    # State, recipient and type each bind independently to the actual cursor
    # issued for this mixed page, without recreating its envelope in a test.
    with pytest.raises(ReadValidationError):
        page(state="unread", cursor=first_twenty.next_cursor)
    with pytest.raises(ReadValidationError):
        page(user_id=bob, cursor=first_twenty.next_cursor)
    with pytest.raises(ReadValidationError):
        page(item_type="status", cursor=first_twenty.next_cursor)
    second_twenty = page(cursor=first_twenty.next_cursor)
    assert len(second_twenty.status_items) == 20 and second_twenty.items == []
    assert chronological_keys(second_twenty)[0] == ("test.secondary", "s10")
    assert [
        i.item_key for i in second_twenty.status_items if i.kind == "test.status"
    ] == [f"p{n:02}" for n in range(11, 23)]
    assert [
        i.item_key for i in second_twenty.status_items if i.kind == "test.secondary"
    ] == [f"s{n:02}" for n in range(10, 18)]
    assert set(chronological_keys(first_twenty)).isdisjoint(
        chronological_keys(second_twenty)
    )
    assert second_twenty.next_cursor is None
    # Both type inputs reuse populated sources; an empty fixture would let a
    # missing filter pass. Archived filtering below uses a different SQL path.
    statuses_only = page(item_type="status")
    assert len(statuses_only.status_items) == 20 and statuses_only.items == []
    messages_only = page(item_type="message")
    assert messages_only.status_items == []
    assert [i.item_key for i in messages_only.items] == ["old"]
    assert messages_only.next_cursor is None
    secondary.clear()

    # Bounded scanning may leave an unread page short, but its cursor must
    # continue beyond read candidates rather than permanently hiding q6.
    # The existing read-mark scenario stopped at page one. Retaining its
    # continuation checks persisted marks together with source advancement,
    # which a cursor codec or an isolated source scan cannot validate.
    status.clear()
    status.update(
        {
            f"q{n}": InboxSnapshot(f"q{n}", "r1", start - timedelta(minutes=n))
            for n in range(1, 7)
        }
    )
    for n in range(1, 5):
        assert mark(f"q{n}", "r1", kind="test.status", read=True).applied
    capped = InboxService(
        InboxRegistry((computed,)),
        listing=InboxListingConfig(page_size_default=2, read_mark_scan_cap=3),
    )

    def capped_page(state, cursor=None):
        with session_factory() as session:
            return capped.list_items(
                session, viewer(alice), state=state, item_type="status", cursor=cursor
            )

    unread = capped_page("unread")
    assert [i.item_key for i in unread.status_items] == ["q5"]
    assert unread.next_cursor is not None
    remaining_unread = capped_page("unread", cursor=unread.next_cursor)
    assert [i.item_key for i in remaining_unread.status_items] == ["q6"]
    assert remaining_unread.next_cursor is None
    everything = capped_page("all")
    assert [i.item_key for i in everything.status_items] == ["q1", "q2"]
    assert everything.next_cursor is not None
    assert capped_page("archived").status_items == []

    # Archive marks also compensate current-state scans. Archived messages
    # page from marks, so q6 is reachable beyond the source's first 2 items.
    assert mark("q1", "r1", kind="test.status", archived=True).applied
    assert mark("q2", "r1", kind="test.status", archived=True).applied
    assert mark("q3", "r1", kind="test.status", archived=True).applied
    assert [i.item_key for i in capped_page("all").status_items] == ["q4", "q5"]
    assert [i.item_key for i in capped_page("unread").status_items] == ["q5"]
    assert mark("q6", "r1", kind="test.status", archived=True).applied
    # Newest archive first, whatever the item's own time; a read mark does
    # not move an archived item. A page asks each source once, not per row.
    assert mark("q2", "r1", kind="test.status", read=False).applied
    newest_archive_first = ["q6", "q3", "q2", "q1", "note"]
    lookups.clear()
    plan = []

    def explain(_conn, cursor, statement, parameters, _context, _many):
        # The Archived page reads its partial index in order, not the
        # recipient's whole history plus a sort.
        if not plan and "ORDER BY workspace_inbox_entry.updated_at DESC" in statement:
            explained = cursor.connection.execute(
                f"EXPLAIN QUERY PLAN {statement}", parameters
            )
            plan.extend(row[-1] for row in explained)

    engine = session_factory.kw["bind"]
    event.listen(engine, "before_cursor_execute", explain)
    try:
        archived_items = page(state="archived").items
    finally:
        event.remove(engine, "before_cursor_execute", explain)
    assert [i.item_key for i in archived_items] == newest_archive_first
    assert len(lookups) == 1
    assert any("ix_workspace_inbox_entry_archived" in step for step in plan)
    assert not any("TEMP B-TREE" in step for step in plan)
    # Archived statuses are persisted marks, so these two type inputs must
    # exercise the repository's SQL filter as well as current-source lookup.
    # The active-page type checks above do not take this query path.
    archived_statuses = page(state="archived", item_type="status")
    assert archived_statuses.status_items == []
    assert [i.item_key for i in archived_statuses.items] == ["q6", "q3", "q2", "q1"]
    archived_messages = page(state="archived", item_type="message")
    assert archived_messages.status_items == []
    assert [i.item_key for i in archived_messages.items] == ["note"]

    def archived_keys(service):
        keys, cursor = [], None
        while True:
            with session_factory() as session:
                result = service.list_items(
                    session, viewer(alice), state="archived", limit=1, cursor=cursor
                )
            assert result.status_items == []
            keys.extend(i.item_key for i in result.items)
            if result.next_cursor is None:
                return keys
            assert result.next_cursor != cursor
            cursor = result.next_cursor

    assert archived_keys(capped) == newest_archive_first
    assert page(user_id=bob, state="archived").items == []
    # Source removal, revision changes and resolver visibility still apply
    # in the archive. Even empty scan batches must leave a usable cursor.
    del status["q1"]
    status["q2"] = InboxSnapshot("q2", "r2", start)
    status["q3"] = InboxSnapshot("q3", "r1", start, {"hidden": True})
    shallow = InboxService(
        InboxRegistry((note, computed)),
        listing=InboxListingConfig(stream_scan_batches=1),
    )
    assert archived_keys(shallow) == ["q6", "note"]
    assert mark("q6", "r1", kind="test.status", archived=False).applied
    assert "q6" in [i.item_key for i in page().status_items]

    # One same-time traversal exercises both kind and item-key collation.
    # Locale ordering of either text column can otherwise hide a successor.
    # Budget exception: the ASCII-only lifecycle cannot expose a mismatch
    # between database and Python tuple ordering. Real ORDER BY and keyset
    # predicates are needed; a Python sort would assume the desired order.
    # This fixture runs on SQLite; it does not prove PostgreSQL locale behavior.
    unicode_producer = InboxService(
        InboxRegistry(
            (
                InboxKind("test.unicode", "workspace", STORED, resolve),
                InboxKind("가", "workspace", STORED, resolve),
            )
        )
    )
    for kind, key in (("test.unicode", "a"), ("가", "a"), ("가", "가")):
        put(key, "v1", 1, service=unicode_producer, kind=kind, user_id=bob)
    unicode_items, cursor = [], None
    while True:
        result = page(user_id=bob, item_type="message", limit=1, cursor=cursor)
        assert len(result.items) == 1 and result.status_items == []
        unicode_items.extend((item.kind, item.item_key) for item in result.items)
        cursor = result.next_cursor
        if cursor is None:
            break
    assert unicode_items == [("가", "가"), ("가", "a"), ("test.unicode", "a")]

    # Final account cleanup closes the recipient to every producer and
    # delivery channel, even when retained history generates a later event.
    with session_factory.begin() as session:
        retire_workspace_recipient(session, workspace_id=workspace_id, user_id=alice)
    previous_deliveries = list(delivered)
    put("after-deletion", "v1", 6)
    # Authorization obtained before deletion cannot recreate a personal mark
    # after waiting for the purge transaction to finish.
    with pytest.raises(ReadNotFoundError):
        mark("q5", "r1", kind="test.status", read=True)
    assert delivered == previous_deliveries
    with session_factory() as session:
        assert (
            session.get(
                WorkspaceInboxEntry,
                (workspace_id, alice, "test.note", "after-deletion"),
            )
            is None
        )
        assert (
            session.get(
                WorkspaceInboxEntry,
                (workspace_id, alice, "test.status", "q5"),
            )
            is None
        )

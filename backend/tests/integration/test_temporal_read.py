"""Bitemporal reads: publish timeline, as_of, what_changed (C2-T8/T9)."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import event, select

from backend.mcp.router import dispatch
from backend.models.change import ChangeOperation, ChangeSet
from backend.models.context import ContextPage, ContextPageVersion
from backend.models.workspace import Workspace
from backend.schemas.context_write import (
    ReviewDecisionRequest,
    WriteDraftRequest,
)
from backend.services.context_write_service import ContextWriteService
from backend.services.review_service import ReviewService
from backend.services.temporal_read import resolve_version_as_of, what_changed

pytestmark = pytest.mark.integration


def _workspace_id(session):
    return session.scalar(select(Workspace.id).where(Workspace.slug == "default"))


def _now():
    return datetime.now(UTC)


_TEMPORAL_EPOCH = datetime(2020, 1, 1, tzinfo=UTC)


def _at(seconds):
    return _TEMPORAL_EPOCH + timedelta(seconds=seconds)


def _pin_version_times(
    session_factory,
    page_id,
    version_id,
    created_at,
    *,
    published_at=None,
):
    # Production history uses wall-clock ChangeSet timestamps; clock steps
    # can reorder or omit events. These tests supply an explicit chronology,
    # not a guarantee of production clock-step safety.
    with session_factory.begin() as session:
        version = session.get(ContextPageVersion, version_id)
        assert version is not None and version.page_id == page_id
        rows = session.execute(
            select(ChangeOperation, ChangeSet)
            .join(ChangeSet, ChangeOperation.change_set_id == ChangeSet.id)
            .where(
                ChangeOperation.target_type == "context_page",
                ChangeOperation.target_id == page_id,
                ChangeOperation.diff_ref == f"context_page_version:{version_id}",
                ChangeOperation.operation.in_(
                    ("create_draft", "create", "update", "publish")
                ),
            )
        ).all()
        assert rows
        if published_at is not None:
            assert published_at > created_at
            assert any(op.operation == "publish" for op, _ in rows)
        version.created_at = created_at
        for operation, change_set in rows:
            instant = (
                published_at
                if operation.operation == "publish" and published_at is not None
                else created_at
            )
            change_set.created_at = instant
            operation.created_at = instant


def _pin_archive_time(session_factory, page_id, archived_at):
    with session_factory.begin() as session:
        operation, change_set = session.execute(
            select(ChangeOperation, ChangeSet)
            .join(ChangeSet, ChangeOperation.change_set_id == ChangeSet.id)
            .where(
                ChangeOperation.target_type == "context_page",
                ChangeOperation.target_id == page_id,
                ChangeOperation.operation == "archive",
            )
        ).one()
        change_set.created_at = archived_at
        operation.created_at = archived_at


def _write_and_publish(session, workspace_id, title, body, *, page_id=None):
    """MCP draft -> human approval, which publishes the latest draft."""
    write = ContextWriteService()
    # write_draft resolves the page by slugified title, so the same title
    # creates a fresh draft on the same page (the re-publish shape).
    created = write.write_draft(
        session=session,
        workspace_id=workspace_id,
        request=WriteDraftRequest(
            title=title,
            page_type="concept",
            body_markdown=body,
            reason=f"publish {title}" if page_id else f"seed {title}",
        ),
        actor_kind="human",
        actor_id="tester",
        actor_role="owner",
    )
    page_id = created.page_id
    version_id = created.draft_version_id
    review_id = created.review_item_id
    if review_id is not None:
        ReviewService().apply_decision(
            session=session,
            workspace_id=workspace_id,
            review_item_id=review_id,
            request=ReviewDecisionRequest(decision_type="approve"),
            actor_kind="human",
            actor_id="tester",
            actor_role="owner",
            actor_provenance="browser_session",
        )

    return page_id, version_id


def test_as_of_resolves_superseded_version(session_factory):
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        before_any = _at(0)
        page_id, v1 = _write_and_publish(
            session, workspace_id, "Temporal Page", "# Temporal\n\nVersion one."
        )
    # Explicit chronology; production clock-step semantics remain unresolved.
    _pin_version_times(session_factory, page_id, v1, _at(10), published_at=_at(20))
    between = _at(30)
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        _, v2 = _write_and_publish(
            session,
            workspace_id,
            "Temporal Page",
            "# Temporal\n\nVersion two.",
            page_id=page_id,
        )
    _pin_version_times(session_factory, page_id, v2, _at(40), published_at=_at(50))

    with session_factory() as session:
        page = session.get(ContextPage, page_id)
        # between the two publishes -> v1 (superseded included)
        resolved = resolve_version_as_of(session, page, between)
        assert resolved is not None and resolved.id == v1
        # before the first publish -> nothing
        assert resolve_version_as_of(session, page, before_any) is None
        # after both publishes -> v2 (the live head)
        head = resolve_version_as_of(session, page, _at(60))
        assert head is not None and head.id == page.current_published_version_id


def test_as_of_uses_publish_instant_not_version_created_at(session_factory):
    """THE pin: a draft created early but published late must resolve by its
    PUBLISH time (version.created_at would wrongly resolve it earlier)."""

    write = ContextWriteService()
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        created = write.write_draft(
            session=session,
            workspace_id=workspace_id,
            request=WriteDraftRequest(
                title="Late Publish",
                page_type="concept",
                body_markdown="# Late\n\nDraft body.",
                reason="draft early",
            ),
            actor_kind="human",
            actor_id="tester",
            actor_role="owner",
        )
    # Explicit chronology; production clock-step semantics remain unresolved.
    draft_created_at = _at(20)
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        if created.review_item_id is not None:
            ReviewService().apply_decision(
                session=session,
                workspace_id=workspace_id,
                review_item_id=created.review_item_id,
                request=ReviewDecisionRequest(decision_type="approve"),
                actor_kind="human",
                actor_id="tester",
                actor_role="owner",
                actor_provenance="browser_session",
            )
    _pin_version_times(
        session_factory,
        created.page_id,
        created.draft_version_id,
        _at(10),
        published_at=_at(30),
    )

    with session_factory() as session:
        page = session.get(ContextPage, created.page_id)
        # at draft_created_at (after draft creation, before publish) the page
        # was NOT published — version.created_at reasoning would say it was
        assert resolve_version_as_of(session, page, draft_created_at) is None
        assert resolve_version_as_of(session, page, _at(40)) is not None


def test_what_changed_delta_flow(session_factory, page_service):
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        page_a, v_a1 = _write_and_publish(
            session, workspace_id, "Delta A", "# A\n\nInitial."
        )
    # Explicit chronology; production clock-step semantics remain unresolved.
    _pin_version_times(session_factory, page_a, v_a1, _at(10), published_at=_at(20))
    capture_point = _at(20)  # The since boundary is exclusive.
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        page_b, v_b1 = _write_and_publish(
            session, workspace_id, "Delta B", "# B\n\nNew page."
        )
        _, v_a2 = _write_and_publish(
            session, workspace_id, "Delta A", "# A\n\nRevised.", page_id=page_a
        )
    _pin_version_times(session_factory, page_b, v_b1, _at(30), published_at=_at(40))
    _pin_version_times(session_factory, page_a, v_a2, _at(50), published_at=_at(60))

    with session_factory() as session:
        workspace_id = _workspace_id(session)
        entries, has_more = what_changed(
            session, workspace_id, since=capture_point, actor=None
        )
    assert [(entry.page_id, entry.version_id) for entry in entries] == [
        (page_b, v_b1),
        (page_a, v_a2),
    ]
    by_page = {entry.page_id: entry for entry in entries}
    assert by_page[page_b].change_kind == "created"
    assert by_page[page_a].change_kind == "updated"
    assert by_page[page_a].reason == "approved through Context review"
    assert not has_more


def test_what_changed_follows_the_published_version(session_factory, page_service):
    """Entries name the version published before them, and a restored
    archived page re-enters the published set."""
    from backend.models.tree import WorkspaceTreeNode
    from backend.services.tree_service import TreeService

    write = ContextWriteService()
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        page_id, v1 = _write_and_publish(
            session, workspace_id, "Head Moves", "# H\n\nOne."
        )
    since = _now()
    with session_factory.begin() as session:
        drafts = [
            write.write_draft(
                session=session,
                workspace_id=workspace_id,
                request=WriteDraftRequest(
                    title="Head Moves",
                    page_type="concept",
                    body_markdown=f"# H\n\n{body}.",
                    reason=f"draft {body}",
                ),
                actor_kind="human",
                actor_id="tester",
                actor_role="owner",
            )
            for body in ("Two", "Three")
        ]
        # Approving the newer draft skips the never-published one below it.
        ReviewService().apply_decision(
            session=session,
            workspace_id=workspace_id,
            review_item_id=drafts[1].review_item_id,
            request=ReviewDecisionRequest(decision_type="approve"),
            actor_kind="human",
            actor_id="tester",
            actor_role="owner",
            actor_provenance="browser_session",
        )
        v3 = drafts[1].draft_version_id
        assert session.get(ContextPageVersion, v3).parent_version_id == (
            drafts[0].draft_version_id
        )
        TreeService().archive_node(
            session=session,
            workspace_id=workspace_id,
            node_id=session.scalar(
                select(WorkspaceTreeNode.id).where(
                    WorkspaceTreeNode.target_kind == "context_page",
                    WorkspaceTreeNode.target_id == page_id,
                )
            ),
            actor_kind="human",
            actor_id="tester",
        )
    # Restoring an older version revives the page; its published version
    # is live again while the restored copy waits as a draft.
    page_service.restore_version(
        page_id=page_id,
        version_id=v1,
        actor_kind="human",
        actor_id="tester",
        actor_provenance="browser_session",
    )

    with session_factory() as session:
        entries, _ = what_changed(session, workspace_id, since=since, actor=None)
        page = session.get(ContextPage, page_id)
        assert resolve_version_as_of(session, page, _now()).id == v3
    assert [
        (e.change_kind, e.version_id, e.previous_version_id, e.reason) for e in entries
    ] == [
        ("updated", v3, v1, "approved through Context review"),
        ("archived", v3, v3, "delete tree node Head Moves"),
        ("created", v3, None, "page restored from archive"),
    ]


def test_what_changed_first_publish_ignores_earlier_draft_create(session_factory):
    write = ContextWriteService()
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        created = write.write_draft(
            session=session,
            workspace_id=workspace_id,
            request=WriteDraftRequest(
                title="First Publish After Since",
                page_type="concept",
                body_markdown="# First Publish\n\nDraft first.",
                reason="draft before delta window",
            ),
            actor_kind="human",
            actor_id="tester",
            actor_role="owner",
        )
    # Explicit chronology; production clock-step semantics remain unresolved.
    since = _at(20)
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        ReviewService().apply_decision(
            session=session,
            workspace_id=workspace_id,
            review_item_id=created.review_item_id,
            request=ReviewDecisionRequest(decision_type="approve"),
            actor_kind="human",
            actor_id="tester",
            actor_role="owner",
            actor_provenance="browser_session",
        )
    _pin_version_times(
        session_factory,
        created.page_id,
        created.draft_version_id,
        _at(10),
        published_at=_at(30),
    )

    with session_factory() as session:
        workspace_id = _workspace_id(session)
        entries, _ = what_changed(session, workspace_id, since=since, actor=None)

    assert [(entry.page_id, entry.change_kind) for entry in entries] == [
        (created.page_id, "created")
    ]


def test_what_changed_uses_one_stable_since_bounded_query(
    session_factory,
):
    """The since-window is materialized by ONE ordered query bounded by
    created_at > since — no OFFSET re-scan per batch (which was not
    snapshot-stable under a concurrent publish)."""
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        since = _now()
        for index in range(3):
            _write_and_publish(
                session,
                workspace_id,
                f"Bounded Delta {index}",
                f"# Bounded {index}\n\nBody.",
            )

    statements: list[str] = []
    with session_factory() as session:
        workspace_id = _workspace_id(session)
        engine = session.get_bind()

        def capture(_conn, _cursor, statement, _parameters, _context, _many):
            statements.append(statement)

        event.listen(engine, "before_cursor_execute", capture)
        try:
            what_changed(session, workspace_id, since=since, actor=None, limit=1)
        finally:
            event.remove(engine, "before_cursor_execute", capture)

    timeline_queries = [
        statement
        for statement in statements
        if "JOIN change_set" in statement
        and "JOIN context_page" in statement
        and "ORDER BY change_set.created_at" in statement
    ]
    assert len(timeline_queries) == 1
    assert "OFFSET" not in timeline_queries[0].upper()
    assert "change_set.created_at >" in timeline_queries[0]


def test_what_changed_mcp_tool_cursor_roundtrip(
    page_service,
    session_factory,
    mcp_default_actor,
):

    # Explicit chronology; production clock-step semantics remain unresolved.
    since = _at(0)
    published = []
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        for i in range(3):
            published.append(
                _write_and_publish(
                    session, workspace_id, f"Cursor {i}", f"# C{i}\n\nBody."
                )
            )

    for i, (page_id, version_id) in enumerate(published):
        _pin_version_times(
            session_factory,
            page_id,
            version_id,
            _at(10 + 20 * i),
            published_at=_at(20 + 20 * i),
        )

    actor = mcp_default_actor(
        actor_id="temporal-cursor-pat",
        scopes=("mcp:read",),
    )
    first = dispatch(
        "what_changed",
        {
            "workspace_id": str(workspace_id),
            "since": since.isoformat(),
            "limit": 2,
        },
        actor=actor,
        page_service=page_service,
    )
    assert first.envelope.result_status == "succeeded"
    assert len(first.entries) == 2
    assert first.has_more and first.next_cursor
    assert [(entry.page_id, entry.version_id) for entry in first.entries] == (
        published[:2]
    )
    assert first.entries[0].stable_url == (f"/p/{workspace_id}/{published[0][0]}")
    assert first.entries[0].link_markdown == (
        f"[Cursor 0](/p/{workspace_id}/{published[0][0]})"
    )
    second = dispatch(
        "what_changed",
        {
            "workspace_id": str(workspace_id),
            "since": since.isoformat(),
            "limit": 2,
            "cursor": first.next_cursor,
        },
        actor=actor,
        page_service=page_service,
    )
    assert second.envelope.result_status == "succeeded"
    assert len(second.entries) == 1
    assert not second.has_more
    assert [(entry.page_id, entry.version_id) for entry in second.entries] == (
        published[2:]
    )
    assert second.entries[0].stable_url == (f"/p/{workspace_id}/{published[2][0]}")
    assert second.entries[0].link_markdown == (
        f"[Cursor 2](/p/{workspace_id}/{published[2][0]})"
    )
    assert second.next_cursor is None


def test_what_changed_rejects_negative_cursor(
    page_service,
    session_factory,
    mcp_default_actor,
):
    import base64

    with session_factory() as session:
        workspace_id = _workspace_id(session)
    actor = mcp_default_actor(
        actor_id="temporal-invalid-cursor-pat",
        scopes=("mcp:read",),
    )
    result = dispatch(
        "what_changed",
        {
            "workspace_id": str(workspace_id),
            "since": _now().isoformat(),
            "cursor": base64.urlsafe_b64encode(b"-1").decode(),
        },
        actor=actor,
        page_service=page_service,
    )

    assert result.envelope.result_status == "failed"
    assert result.envelope.error_code == "validation_failed"


def test_archive_surfaces_as_archived_delta(
    session_factory, page_service, mcp_default_actor
):
    from backend.models.tree import WorkspaceTreeNode
    from backend.services.tree_service import TreeService

    payload = page_service.write_page(
        workspace_slug="default",
        slug="delta-archived",
        title="Delta Archived",
        body_md="# DA\n\nBody.",
        actor_kind="human",
        actor_id="tester",
        actor_provenance="browser_session",
    )
    # Explicit chronology; production clock-step semantics remain unresolved.
    _pin_version_times(session_factory, payload.id, payload.version_id, _at(10))
    since = _at(20)
    tree = TreeService()
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        node = session.scalar(
            select(WorkspaceTreeNode).where(
                WorkspaceTreeNode.target_kind == "context_page",
                WorkspaceTreeNode.target_id == payload.id,
            )
        )
        tree.archive_node(
            session=session,
            workspace_id=workspace_id,
            node_id=node.id,
            actor_kind="human",
            actor_id="tester",
        )
    _pin_archive_time(session_factory, payload.id, _at(30))

    with session_factory() as session:
        workspace_id = _workspace_id(session)
        entries, _ = what_changed(session, workspace_id, since=since, actor=None)
    archived = [e for e in entries if e.page_id == payload.id]
    assert len(archived) == 1
    assert archived[0].change_kind == "archived"

    mcp_result = dispatch(
        "what_changed",
        {"workspace_id": str(workspace_id), "since": since.isoformat()},
        actor=mcp_default_actor(scopes=("mcp:read",)),
        page_service=page_service,
    )
    mcp_archived = [
        entry for entry in mcp_result.entries if entry.page_id == payload.id
    ]
    assert len(mcp_archived) == 1
    assert mcp_archived[0].change_kind == "archived"
    assert mcp_archived[0].stable_url is None
    assert mcp_archived[0].link_markdown is None

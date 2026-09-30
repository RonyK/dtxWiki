from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from sqlalchemy import select

from backend.models.ask import AskSession
from backend.models.lint import LintFinding, LintRun
from backend.models.workspace import Workspace
from backend.schemas.ask import AskSessionCreate
from backend.services.ask_service import AskService
from backend.services.lint_service import LintService
from backend.services.list_pagination import (
    _decode_keyset_cursor,
    _encode_keyset_cursor,
)

ACTOR_ID = "00000000-0000-0000-0000-000000000010"


def _default_workspace(session) -> Workspace:
    workspace = session.scalar(select(Workspace).where(Workspace.slug == "default"))
    assert workspace is not None
    return workspace


def test_keyset_cursor_roundtrip_and_tolerance():
    ts = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    item_id = UUID("00000000-0000-0000-0000-000000000abc")
    token = _encode_keyset_cursor(ts, item_id)
    assert _decode_keyset_cursor(token) == (ts, item_id)
    # Tolerant: empty/garbled cursors restart at the first page (None).
    assert _decode_keyset_cursor(None) is None
    assert _decode_keyset_cursor("") is None
    assert _decode_keyset_cursor("not-a-valid-token!!!") is None


def test_ask_list_sessions_keyset_paginates_to_older_rows(session_factory):
    service = AskService()
    base = datetime(2026, 1, 1, tzinfo=UTC)
    with session_factory.begin() as session:
        workspace = _default_workspace(session)
        for index in range(7):
            service.create_session(
                session=session,
                workspace_id=workspace.id,
                request=AskSessionCreate(title=f"Session {index:02d}"),
                actor_kind="human",
                actor_id=ACTOR_ID,
            )
        # Spread updated_at so the keyset order is deterministic.
        for offset, ask_session in enumerate(
            session.scalars(select(AskSession)).all()
        ):
            ask_session.updated_at = base + timedelta(minutes=offset)

    with session_factory() as session:
        first = service.list_sessions(
            session=session, workspace_id=workspace.id, limit=5
        )
        assert len(first.items) == 5
        assert first.total == 7
        assert first.has_more is True
        assert first.next_cursor is not None

        second = service.list_sessions(
            session=session,
            workspace_id=workspace.id,
            limit=5,
            cursor=first.next_cursor,
        )

    assert len(second.items) == 2
    assert second.has_more is False
    assert second.next_cursor is None
    first_ids = {item.id for item in first.items}
    second_ids = {item.id for item in second.items}
    assert first_ids.isdisjoint(second_ids)


def test_ask_list_sessions_keyset_handles_tied_updated_at(session_factory):
    # Regression: the keyset cursor compares (updated_at, id), so the ORDER BY
    # must be exactly (updated_at desc, id desc). When every row shares one
    # updated_at and started_at order disagrees with id order, an extra
    # started_at term in the ORDER BY would skip and duplicate rows across
    # pages. Paginating must still return every session exactly once.
    service = AskService()
    base = datetime(2026, 1, 1, tzinfo=UTC)
    with session_factory.begin() as session:
        workspace = _default_workspace(session)
        for index in range(6):
            service.create_session(
                session=session,
                workspace_id=workspace.id,
                request=AskSessionCreate(title=f"Session {index:02d}"),
                actor_kind="human",
                actor_id=ACTOR_ID,
            )
        seeded = session.scalars(select(AskSession)).all()
        # Same updated_at for every row; started_at ordered opposite to the id
        # sort so a started_at-based ORDER BY would diverge from the cursor.
        for rank, ask_session in enumerate(
            sorted(seeded, key=lambda row: row.id)
        ):
            ask_session.updated_at = base
            ask_session.started_at = base + timedelta(minutes=len(seeded) - rank)
        all_ids = {ask_session.id for ask_session in seeded}

    collected: list[UUID] = []
    cursor: str | None = None
    with session_factory() as session:
        for _ in range(10):  # generous page cap; loop ends via has_more
            page = service.list_sessions(
                session=session, workspace_id=workspace.id, limit=2, cursor=cursor
            )
            collected.extend(item.id for item in page.items)
            if not page.has_more:
                break
            cursor = page.next_cursor

    assert len(collected) == len(all_ids)  # no rows skipped
    assert len(set(collected)) == len(collected)  # no duplicates
    assert set(collected) == all_ids


def test_ask_list_sessions_garbled_cursor_restarts(session_factory):
    service = AskService()
    with session_factory.begin() as session:
        workspace = _default_workspace(session)
        for index in range(3):
            service.create_session(
                session=session,
                workspace_id=workspace.id,
                request=AskSessionCreate(title=f"Session {index}"),
                actor_kind="human",
                actor_id=ACTOR_ID,
            )

    with session_factory() as session:
        first = service.list_sessions(
            session=session, workspace_id=workspace.id, limit=10
        )
        garbled = service.list_sessions(
            session=session,
            workspace_id=workspace.id,
            limit=10,
            cursor="garbled",
        )

    assert [item.id for item in garbled.items] == [item.id for item in first.items]
    assert garbled.total == 3


def _seed_lint_findings(session, workspace_id, count, *, severity="warning"):
    lint_run = LintRun(
        workspace_id=workspace_id,
        lint_scope="workspace",
        trigger_type="manual",
        status="completed",
    )
    session.add(lint_run)
    session.flush()
    base = datetime(2026, 1, 1, tzinfo=UTC)
    for index in range(count):
        session.add(
            LintFinding(
                lint_run_id=lint_run.id,
                workspace_id=workspace_id,
                target_type="context_page",
                rule_id="missing_citation",
                severity=severity,
                status="open",
                created_at=base + timedelta(minutes=index),
            )
        )
    session.flush()


def test_lint_list_findings_keyset_paginates_to_older_rows(session_factory):
    service = LintService()
    with session_factory.begin() as session:
        workspace = _default_workspace(session)
        _seed_lint_findings(session, workspace.id, 14)

    with session_factory() as session:
        first = service.list_findings(
            session=session, workspace_id=workspace.id, limit=10
        )
        assert len(first.findings) == 10
        assert first.total == 14
        assert first.has_more is True
        assert first.next_cursor is not None

        second = service.list_findings(
            session=session,
            workspace_id=workspace.id,
            limit=10,
            cursor=first.next_cursor,
        )

    assert len(second.findings) == 4
    assert second.has_more is False
    assert second.next_cursor is None
    first_ids = {finding.id for finding in first.findings}
    second_ids = {finding.id for finding in second.findings}
    assert first_ids.isdisjoint(second_ids)


def test_lint_list_findings_garbled_cursor_restarts(session_factory):
    service = LintService()
    with session_factory.begin() as session:
        workspace = _default_workspace(session)
        _seed_lint_findings(session, workspace.id, 5)

    with session_factory() as session:
        first = service.list_findings(
            session=session, workspace_id=workspace.id, limit=10
        )
        garbled = service.list_findings(
            session=session,
            workspace_id=workspace.id,
            limit=10,
            cursor="not-real",
        )

    assert [f.id for f in garbled.findings] == [f.id for f in first.findings]
    assert garbled.total == 5


@pytest.mark.parametrize(
    ("count", "expected_page_count", "expected_last_page_size"),
    [
        (0, 0, 0),
        (1, 1, 1),
        (20, 1, 20),
        (21, 2, 1),
        (39, 2, 19),
        (101, 6, 1),
    ],
)
def test_lint_numeric_pagination_metadata_and_boundaries(
    session_factory,
    count,
    expected_page_count,
    expected_last_page_size,
):
    service = LintService()
    with session_factory.begin() as session:
        workspace = _default_workspace(session)
        _seed_lint_findings(session, workspace.id, count)

    with session_factory() as session:
        first = service.list_findings(
            session=session,
            workspace_id=workspace.id,
            page=1,
            page_size=20,
        )
        last = (
            service.list_findings(
                session=session,
                workspace_id=workspace.id,
                page=expected_page_count,
                page_size=20,
            )
            if expected_page_count > 0
            else first
        )

    assert first.total == count
    assert first.page == 1
    assert first.page_size == 20
    assert first.page_count == expected_page_count
    assert first.has_previous is False
    assert first.has_next is (expected_page_count > 1)
    assert first.has_more is first.has_next
    assert first.next_cursor is None
    assert len(first.findings) == min(count, 20)
    assert len(last.findings) == expected_last_page_size
    assert last.has_next is False
    assert last.has_more is False
    assert last.next_cursor is None
    assert last.has_previous is (expected_page_count > 1)


def test_lint_numeric_pagination_rejects_cursor_mode_parameters(session_factory):
    service = LintService()
    with session_factory() as session:
        workspace = _default_workspace(session)
        with pytest.raises(
            ValueError,
            match="page/page_size cannot be combined with limit/cursor",
        ):
            service.list_findings(
                session=session,
                workspace_id=workspace.id,
                page=1,
                page_size=20,
                limit=50,
            )

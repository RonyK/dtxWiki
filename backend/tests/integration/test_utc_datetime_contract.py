"""Regression: timestamps must serialize with a UTC offset on every dialect.

SQLite has no native timezone type, so a plain ``DateTime(timezone=True)``
column round-trips UTC instants as *naive* datetimes. Pydantic then serializes
them without an offset (e.g. "2026-01-01T12:00:00.119886") and JS clients parse
that as *local* time, shifting every displayed timestamp by the viewer's UTC
offset. The ``UTCDateTime`` TypeDecorator re-attaches UTC on read so the wire
contract always carries "+00:00". These tests pin that contract against the real
service read path (fresh session -> ORM load -> Pydantic JSON), not a hand-built
aware fixture that would mask the bug.
"""

from datetime import UTC, datetime

from sqlalchemy import select

from backend.models.ask import AskSession
from backend.models.base import UTCDateTime
from backend.schemas.ask import AskSessionCreate
from backend.services.ask_service import AskService

ACTOR_ID = "00000000-0000-0000-0000-000000000010"
# Deliberately naive (no tzinfo) to mimic what SQLite hands back from storage.
NAIVE_INSTANT = datetime(2026, 1, 1, 12, 0, 0, 119886)


def _default_workspace_id(session):
    from backend.models.workspace import Workspace

    workspace = session.scalar(select(Workspace).where(Workspace.slug == "default"))
    assert workspace is not None
    return workspace.id


def test_utcdatetime_reattaches_utc_on_naive_result_value():
    # Unit-level: a naive value read from any dialect comes back UTC-aware.
    decorator = UTCDateTime()
    bound = decorator.process_bind_param(NAIVE_INSTANT, dialect=None)
    assert bound.tzinfo is UTC  # naive write is normalized to UTC
    result = decorator.process_result_value(NAIVE_INSTANT, dialect=None)
    assert result.tzinfo is UTC  # naive read is normalized to UTC
    # An already-aware value is passed through untouched.
    aware = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    assert decorator.process_result_value(aware, dialect=None) is aware


def test_ask_session_timestamps_serialize_with_utc_offset(session_factory):
    service = AskService()
    with session_factory.begin() as session:
        workspace_id = _default_workspace_id(session)
        service.create_session(
            session=session,
            workspace_id=workspace_id,
            request=AskSessionCreate(title="Naive timestamp session"),
            actor_kind="human",
            actor_id=ACTOR_ID,
        )
        # Overwrite with naive datetimes so the row stored on SQLite has no
        # offset, exactly as a pre-fix instant would. The bind param normalizes
        # them to UTC; the read path must hand them back offset-aware.
        ask_session = session.scalar(select(AskSession))
        ask_session.started_at = NAIVE_INSTANT
        ask_session.updated_at = NAIVE_INSTANT

    # Fresh session forces a load from the DB (no identity-map shortcut), so the
    # TypeDecorator's process_result_value runs — the real served path.
    with session_factory() as session:
        page = service.list_sessions(session=session, workspace_id=workspace_id)

    assert len(page.items) == 1
    item = page.items[0]
    assert item.started_at.tzinfo is not None
    assert item.updated_at.tzinfo is not None

    payload = item.model_dump(mode="json")
    # The wire contract: a UTC offset marker must be present so clients do not
    # silently reinterpret the instant as local time. Pydantic serializes UTC as
    # a trailing "Z"; Postgres-tier values arrive as "+00:00". Both are accepted
    # by the frontend's normalizeTimestamp (its hasOffset test covers [zZ] and
    # ±HH:MM); a naive "...119886" with no marker is the bug this guards against.
    for field in ("started_at", "updated_at"):
        served = payload[field]
        assert served.endswith("Z") or served.endswith("+00:00"), served

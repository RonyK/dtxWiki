"""Storage of one scope's entry table: stored messages and personal marks.

Only this module knows the table's row modes. A ``stored`` row is a message a
producer wrote, with the recipient's marks; a ``mark`` row holds the marks for
an item that is derived on read.
"""

from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime
from typing import Any, Protocol
from uuid import UUID

from sqlalchemy import and_, inspect, not_, or_, select
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from backend.models.base import utc_now
from backend.services.inbox.base import InboxScope, InboxSnapshot, InboxState

STORED_MODE = "stored"
MARK_MODE = "mark"


class InboxEntryRow(Protocol):
    """One row of a scope's entry table."""

    kind: str
    item_key: str
    mode: str
    revision: str | None
    ref: dict[str, Any] | None
    event_at: datetime | None
    read_revision: str | None
    archived_revision: str | None
    updated_at: datetime


def upsert_stored(
    session: Session,
    scope: InboxScope,
    kind: str,
    recipient_ids: Iterable[UUID],
    snapshot: InboxSnapshot,
) -> None:
    """Create or update a stored row per recipient, keeping their marks."""

    now = utc_now()
    rows = [
        {
            **scope.owner(recipient_id),
            "kind": kind,
            "item_key": snapshot.item_key,
            "mode": STORED_MODE,
            "revision": snapshot.revision,
            "ref": dict(snapshot.ref) if snapshot.ref is not None else None,
            "event_at": snapshot.event_at,
            "updated_at": now,
        }
        # A fixed order keeps concurrent producers' row locks in one order.
        for recipient_id in sorted(set(recipient_ids))
    ]
    if not rows:
        return
    statement = _insert(session, scope.model).values(rows)
    session.execute(
        statement.on_conflict_do_update(
            index_elements=_key_columns(scope.model),
            set_={
                name: statement.excluded[name]
                for name in ("mode", "revision", "ref", "event_at", "updated_at")
            },
        )
    )


def stored_snapshot(entry: InboxEntryRow) -> InboxSnapshot | None:
    """The message a stored row holds; None for a marks-only row."""

    if entry.mode != STORED_MODE or entry.revision is None:
        return None
    return InboxSnapshot(entry.item_key, entry.revision, entry.event_at, entry.ref)


def lock_entry(
    session: Session, scope: InboxScope, user_id: UUID, kind: str, item_key: str
) -> InboxEntryRow:
    """Ensure the viewer's row for an item exists and lock it.

    A missing row is created marks-only. The caller rolls back if the item
    turns out not to exist, so no row survives for an unknown key.
    """

    key = {**scope.owner(user_id), "kind": kind, "item_key": item_key}
    session.execute(
        _insert(session, scope.model)
        .values(**key, mode=MARK_MODE, updated_at=utc_now())
        .on_conflict_do_nothing(index_elements=_key_columns(scope.model))
    )
    return session.scalars(
        select(scope.model)
        .where(*_equal(scope.model, key))
        .with_for_update()
        .execution_options(populate_existing=True)
    ).one()


def marks_for(
    session: Session,
    scope: InboxScope,
    user_id: UUID,
    kind: str,
    item_keys: Sequence[str],
) -> dict[str, InboxEntryRow]:
    model = scope.model
    return {
        entry.item_key: entry
        for entry in session.scalars(
            select(model).where(
                *_equal(model, scope.owner(user_id)),
                model.kind == kind,
                model.item_key.in_(item_keys),
            )
        )
    }


def stored_page(
    session: Session,
    scope: InboxScope,
    user_id: UUID,
    *,
    state: InboxState,
    reopen_kinds: Sequence[str],
    after: tuple[datetime, str, str] | None,
    limit: int,
) -> Sequence[InboxEntryRow]:
    """Stored rows in keyset order (event_at, kind, item_key), newest first."""

    model = scope.model
    archived = and_(
        model.archived_revision.is_not(None),
        or_(
            model.kind.not_in(reopen_kinds),
            model.archived_revision == model.revision,
        ),
    )
    conditions = [
        *_equal(model, scope.owner(user_id)),
        model.mode == STORED_MODE,
        archived if state == "archived" else not_(archived),
    ]
    if state == "unread":
        conditions.append(
            or_(model.read_revision.is_(None), model.read_revision != model.revision)
        )
    if after is not None:
        event_at, kind, item_key = after
        conditions.append(
            or_(
                model.event_at < event_at,
                and_(
                    model.event_at == event_at,
                    or_(
                        model.kind < kind,
                        and_(model.kind == kind, model.item_key < item_key),
                    ),
                ),
            )
        )
    return session.scalars(
        select(model)
        .where(*conditions)
        .order_by(model.event_at.desc(), model.kind.desc(), model.item_key.desc())
        .limit(limit)
    ).all()


def _equal(model: type, values: Mapping[str, Any]) -> list:
    return [getattr(model, name) == value for name, value in values.items()]


def _key_columns(model: type) -> list[str]:
    return [column.name for column in inspect(model).primary_key]


def _insert(session: Session, model: type):
    dialect = session.get_bind().dialect.name
    inserts = {"postgresql": postgresql_insert, "sqlite": sqlite_insert}
    if dialect not in inserts:
        raise RuntimeError(f"inbox writes are not supported on {dialect}")
    return inserts[dialect](model)

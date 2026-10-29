"""Inbox operations over the contracts in ``base``: write, list and mark.

Nothing here knows a concrete kind, source, scope or channel. The viewer's
scope supplies the table, owner key, write lock and link rule; each kind's
source supplies its items; each kind's resolver decides what the viewer sees;
listeners react to written items. Table details stay in ``entries``.
"""

import base64
import binascii
import json
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from backend.models.base import utc_now
from backend.services.inbox import entries
from backend.services.inbox.base import (
    InboxDisplay,
    InboxItem,
    InboxKind,
    InboxListener,
    InboxMarkResult,
    InboxOverflow,
    InboxPage,
    InboxScope,
    InboxSnapshot,
    InboxState,
    InboxViewer,
)
from backend.services.inbox.entries import InboxEntryRow
from backend.services.inbox.registry import UNREGISTERED_DISPLAY, InboxRegistry
from backend.services.read_errors import ReadNotFoundError, ReadValidationError

# Upper bound on what one kind's status scan returns per list request.
STATUS_ITEM_LIMIT = 20
# A page reads ahead at most this many batches past items the viewer cannot see.
_SCAN_BATCHES = 5


class InboxService:
    def __init__(
        self, registry: InboxRegistry, listeners: Iterable[InboxListener] = ()
    ) -> None:
        self.registry = registry
        self.listeners = tuple(listeners)

    def put(
        self,
        session: Session,
        *,
        scope: InboxScope,
        recipient_ids: Iterable[UUID],
        kind: str,
        item_key: str,
        revision: str,
        event_at: datetime,
        ref: Mapping[str, Any] | None = None,
    ) -> None:
        """Create or update an item for each recipient, in the caller's
        transaction. The kind's source decides whether it can be written."""

        if not item_key or not revision:
            raise ValueError("inbox item_key and revision must be non-empty")
        spec = self.registry.writable(scope.name, kind)
        recipients = tuple(sorted(set(recipient_ids)))
        if not recipients:
            return
        snapshot = InboxSnapshot(item_key, revision, event_at, ref)
        spec.source.write(session, scope, spec.name, recipients, snapshot)
        for listener in self.listeners:
            listener.on_put(session, scope, spec.name, recipients, snapshot)

    def list_items(
        self,
        session: Session,
        viewer: InboxViewer,
        *,
        state: InboxState = "all",
        cursor: str | None = None,
        limit: int = 30,
    ) -> InboxPage:
        """Status items come whole on the first page; stored items page by
        keyset, reading ahead past items the viewer can no longer see."""

        scope = viewer.scope
        kinds = self.registry.in_scope(scope.name)
        status_items: list[InboxItem] = []
        status_overflow: list[InboxOverflow] = []
        if cursor is None:
            for spec in self.registry.status_kinds(scope.name):
                items, overflow = self._status(session, viewer, spec, state)
                status_items.extend(items)
                status_overflow.extend(overflow)
            status_items.sort(key=_newest_first)

        reopen = [name for name, spec in kinds.items() if spec.reopen_on_new_revision]
        position = _decode_cursor(cursor) if cursor is not None else None
        items: list[InboxItem] = []
        last: InboxEntryRow | None = None
        for _ in range(_SCAN_BATCHES):
            rows = entries.stored_page(
                session,
                scope,
                viewer.user_id,
                state=state,
                reopen_kinds=reopen,
                after=position,
                limit=limit + 1,
            )
            displays = self._resolve_rows(session, viewer, rows)
            for row in rows:
                if len(items) == limit:
                    return InboxPage(
                        status_items, status_overflow, items, _encode_cursor(last)
                    )
                last = row
                display = displays.get((row.kind, row.item_key))
                snapshot = entries.stored_snapshot(row)
                if display is not None and snapshot is not None:
                    spec = self.registry.kind(scope.name, row.kind)
                    items.append(_item(spec, snapshot, row, display))
            if len(rows) <= limit:
                return InboxPage(status_items, status_overflow, items, None)
            position = (last.event_at, last.kind, last.item_key)
        return InboxPage(status_items, status_overflow, items, _encode_cursor(last))

    def mark(
        self,
        session: Session,
        viewer: InboxViewer,
        *,
        kind: str,
        item_key: str,
        seen_revision: str,
        read: bool | None = None,
        archived: bool | None = None,
    ) -> InboxMarkResult:
        """Set the viewer's marks for the revision they saw.

        One procedure for every kind: take the scope's write lock, lock the
        viewer's row for the item, then ask the kind's source for the current
        state. A missing or invisible item raises not-found, which rolls back
        any row created for it. A valid item whose revision moved on is left
        unchanged and reported as not applied, so a late request cannot mark
        newer news as read.
        """

        if read is None and archived is None:
            raise ReadValidationError("set read or archived")
        scope = viewer.scope
        spec = self.registry.kind(scope.name, kind)
        scope.acquire_write(session)
        # A savepoint removes the row created for a missing item, whatever the
        # caller then does with its transaction.
        with session.begin_nested():
            entry = entries.lock_entry(session, scope, viewer.user_id, kind, item_key)
            current = spec.source.current(
                session, viewer, kind, item_key, entries.stored_snapshot(entry)
            )
            display = (
                self._resolve(session, viewer, spec, [current]).get(item_key)
                if current is not None
                else None
            )
            if current is None or display is None:
                raise ReadNotFoundError("inbox item not found")

        applied = seen_revision == current.revision
        if applied:
            if read is not None:
                entry.read_revision = current.revision if read else None
            if archived is not None:
                entry.archived_revision = current.revision if archived else None
            entry.updated_at = utc_now()
            session.flush()
        return InboxMarkResult(applied, _item(spec, current, entry, display))

    def _status(
        self, session: Session, viewer: InboxViewer, spec: InboxKind, state: InboxState
    ) -> tuple[list[InboxItem], list[InboxOverflow]]:
        scan = spec.source.status(session, viewer, spec.name, STATUS_ITEM_LIMIT)
        found = list(scan.items)[:STATUS_ITEM_LIMIT]
        overflow = (
            [InboxOverflow(spec.name, scan.total)] if scan.total > len(found) else []
        )
        if not found:
            return [], overflow
        marks = entries.marks_for(
            session,
            viewer.scope,
            viewer.user_id,
            spec.name,
            [snapshot.item_key for snapshot in found],
        )
        displays = self._resolve(session, viewer, spec, found)
        items = [
            _item(spec, snapshot, marks.get(snapshot.item_key), display)
            for snapshot in found
            if (display := displays.get(snapshot.item_key)) is not None
        ]
        return [item for item in items if _matches(item, state)], overflow

    def _resolve_rows(
        self, session: Session, viewer: InboxViewer, rows: Sequence[InboxEntryRow]
    ) -> dict[tuple[str, str], InboxDisplay]:
        by_kind: dict[str, list[InboxSnapshot]] = {}
        for row in rows:
            snapshot = entries.stored_snapshot(row)
            if snapshot is not None:
                by_kind.setdefault(row.kind, []).append(snapshot)
        displays: dict[tuple[str, str], InboxDisplay] = {}
        for name, snapshots in by_kind.items():
            spec = self.registry.kind(viewer.scope.name, name)
            for item_key, display in self._resolve(
                session, viewer, spec, snapshots
            ).items():
                displays[(name, item_key)] = display
        return displays

    def _resolve(
        self,
        session: Session,
        viewer: InboxViewer,
        spec: InboxKind,
        snapshots: Sequence[InboxSnapshot],
    ) -> Mapping[str, InboxDisplay]:
        resolved = spec.resolve(session, viewer, snapshots)
        displays = {
            snapshot.item_key: display
            for snapshot in snapshots
            if (display := resolved.get(snapshot.item_key)) is not None
        }
        for display in displays.values():
            if display is not UNREGISTERED_DISPLAY:
                viewer.scope.check_display(display)
        return displays


def _item(
    spec: InboxKind,
    snapshot: InboxSnapshot,
    entry: InboxEntryRow | None,
    display: InboxDisplay,
) -> InboxItem:
    read_revision = entry.read_revision if entry is not None else None
    archived_revision = entry.archived_revision if entry is not None else None
    return InboxItem(
        kind=spec.name,
        item_key=snapshot.item_key,
        revision=snapshot.revision,
        event_at=snapshot.event_at,
        unread=read_revision != snapshot.revision,
        archived=archived_revision is not None
        and (not spec.reopen_on_new_revision or archived_revision == snapshot.revision),
        display=None if display is UNREGISTERED_DISPLAY else display,
    )


def _matches(item: InboxItem, state: InboxState) -> bool:
    if state == "archived":
        return item.archived
    return not item.archived and (state == "all" or item.unread)


def _newest_first(item: InboxItem) -> tuple[float, str, str]:
    stamp = item.event_at.timestamp() if item.event_at is not None else 0.0
    return (-stamp, item.kind, item.item_key)


def _encode_cursor(entry: InboxEntryRow) -> str:
    raw = json.dumps([entry.event_at.isoformat(), entry.kind, entry.item_key])
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def _decode_cursor(cursor: str) -> tuple[datetime, str, str]:
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        event_at, kind, item_key = json.loads(raw)
        return datetime.fromisoformat(event_at), str(kind), str(item_key)
    except (binascii.Error, ValueError, TypeError) as exc:
        raise ReadValidationError("invalid inbox cursor") from exc

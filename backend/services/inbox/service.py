"""Inbox operations over the contracts in ``base``: write, list and mark.

Nothing here knows a concrete kind, source, scope or channel. The viewer's
scope supplies the table, owner key, write lock and link rule; each kind's
source supplies its items; each kind's resolver decides what the viewer sees;
listeners react to written items. Table details stay in ``entries``.
"""

from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID

from sqlalchemy.orm import Session

from backend.models.base import utc_now
from backend.services.db_read_snapshot import acquire_consistent_read_snapshot
from backend.services.inbox import entries
from backend.services.inbox.base import (
    InboxDisplay,
    InboxItem,
    InboxKind,
    InboxListener,
    InboxMarkResult,
    InboxPage,
    InboxPosition,
    InboxScope,
    InboxSnapshot,
    InboxState,
    InboxViewer,
)
from backend.services.inbox.config import DEFAULT_INBOX_CONFIG, InboxListingConfig
from backend.services.inbox.cursors import decode_cursor, encode_cursor, parse_position
from backend.services.inbox.entries import InboxEntryRow
from backend.services.inbox.registry import UNREGISTERED_DISPLAY, InboxRegistry
from backend.services.read_errors import ReadNotFoundError, ReadValidationError


class InboxService:
    def __init__(
        self,
        registry: InboxRegistry,
        listeners: Iterable[InboxListener] = (),
        *,
        listing: InboxListingConfig = DEFAULT_INBOX_CONFIG.listing,
    ) -> None:
        self.registry = registry
        self.listeners = tuple(listeners)
        self.listing = listing

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
        transaction. Producers hold their source locks before this call; the
        scope then locks and filters recipients before entries or delivery
        outboxes are written. The kind's source decides whether it is writable.
        """

        if not item_key or not revision:
            raise ValueError("inbox item_key and revision must be non-empty")
        spec = self.registry.writable(scope.name, kind)
        recipients = tuple(sorted(set(recipient_ids)))
        if not recipients:
            return
        recipients = scope.writable_recipients(session, recipients)
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
        item_type: Literal["all", "status", "message"] = "all",
        cursor: str | None = None,
        limit: int | None = None,
    ) -> InboxPage:
        """Merge bounded source pages into one chronological inbox page.

        Current items and stored messages share the same keyset boundary.
        Archived rows retain their archive-time order and batched hydration.
        """

        acquire_consistent_read_snapshot(session)
        if limit is None:
            limit = self.listing.page_size_default
        if not 1 <= limit <= self.listing.page_size_max:
            raise ReadValidationError("invalid inbox page size")
        if item_type not in ("all", "status", "message"):
            raise ReadValidationError("invalid inbox item type")
        position = _decode_cursor(cursor, viewer, state, item_type) if cursor else None
        scope = viewer.scope
        kinds = self.registry.in_scope(scope.name)
        status_kinds = self.registry.status_kinds(scope.name)
        status_names = [spec.name for spec in status_kinds]
        reopen = [name for name, spec in kinds.items() if spec.reopen_on_new_revision]
        status_items: list[InboxItem] = []
        items: list[InboxItem] = []
        # Batch independently of the requested visible limit: limit=1 must
        # not turn a long run of read marks into one source query per row.
        budget = limit + self.listing.read_mark_scan_cap
        for _ in range(self.listing.stream_scan_batches):
            batch_size = min(max(limit, self.listing.page_size_default), budget)
            candidates: list[tuple[InboxPosition, InboxSnapshot | InboxEntryRow]] = []
            more = False
            if state != "archived" and item_type != "message":
                for spec in status_kinds:
                    scan = spec.source.status(
                        session, viewer, spec.name, batch_size, position
                    )
                    more = more or scan.has_more
                    candidates.extend(
                        (
                            _position(snapshot.event_at, spec.name, snapshot.item_key),
                            snapshot,
                        )
                        for snapshot in scan.items
                    )
            if state == "archived" or item_type != "status":
                rows = entries.message_page(
                    session,
                    scope,
                    viewer.user_id,
                    state=state,
                    reopen_kinds=reopen,
                    after=position,
                    limit=batch_size + 1,
                    item_type=item_type,
                    status_kinds=status_names,
                )
                candidates.extend(
                    (_position(*entries.list_position(row, state)), row) for row in rows
                )
            candidates.sort(key=lambda candidate: candidate[0], reverse=True)
            more = more or len(candidates) > batch_size
            batch = candidates[:batch_size]
            if not batch:
                if more:
                    raise ValueError(
                        "inbox source returned continuation without candidates"
                    )
                return InboxPage(status_items, items, None)
            selected_rows = [
                row for _, row in batch if not isinstance(row, InboxSnapshot)
            ]
            resolved = self._resolve_rows(session, viewer, selected_rows)
            status_batches: dict[str, list[InboxSnapshot]] = {}
            for (_, kind, _), candidate in batch:
                if isinstance(candidate, InboxSnapshot):
                    status_batches.setdefault(kind, []).append(candidate)
            for kind, snapshots in status_batches.items():
                for item in self._status_items(
                    session, viewer, kinds[kind], state, snapshots
                ):
                    resolved[(kind, item.item_key)] = item
            for at, candidate in batch:
                found = resolved.get((at[1], at[2]))
                if found is not None:
                    if isinstance(found, InboxItem):
                        item = found
                    else:
                        snapshot, display = found
                        item = _item(
                            self.registry.kind(scope.name, at[1]),
                            snapshot,
                            candidate,
                            display,
                        )
                    if _matches(item, state):
                        if len(status_items) + len(items) == limit:
                            return InboxPage(
                                status_items,
                                items,
                                _encode_cursor(position, viewer, state, item_type),
                            )
                        (
                            status_items
                            if isinstance(candidate, InboxSnapshot)
                            else items
                        ).append(item)
                # A filtered tail is consumed even after this page fills.
                # Only an eligible following row requires another page.
                position = at
            if not more:
                return InboxPage(status_items, items, None)
            budget -= len(batch)
            if budget <= 0:
                break
        return InboxPage(
            status_items, items, _encode_cursor(position, viewer, state, item_type)
        )

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

        One procedure for every kind: take the scope's write lock, lock its
        recipient lifecycle, lock the viewer's row for the item, then ask the
        kind's source for the current state. A missing or invisible item raises
        not-found, which rolls back any row created for it. A valid item whose
        revision moved on is left unchanged and reported as not applied, so a
        late request cannot mark newer news as read.
        """

        if read is None and archived is None:
            raise ReadValidationError("set read or archived")
        scope = viewer.scope
        spec = self.registry.kind(scope.name, kind)
        scope.acquire_write(session)
        # A savepoint removes any recipient and entry rows created for a missing
        # item, whatever the caller then does with its transaction.
        with session.begin_nested():
            if not scope.writable_recipients(session, (viewer.user_id,)):
                raise ReadNotFoundError("inbox item not found")
            entry = entries.lock_entry(session, scope, viewer.user_id, kind, item_key)
            current = spec.source.current(
                session, viewer, kind, {item_key: entries.stored_snapshot(entry)}
            ).get(item_key)
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
                # Orders the Archived list; a read mark does not move an item.
                entry.updated_at = utc_now()
            session.flush()
        return InboxMarkResult(applied, _item(spec, current, entry, display))

    def _status_items(
        self,
        session: Session,
        viewer: InboxViewer,
        spec: InboxKind,
        state: InboxState,
        snapshots: Sequence[InboxSnapshot],
    ) -> list[InboxItem]:
        marks = entries.marks_for(
            session,
            viewer.scope,
            viewer.user_id,
            spec.name,
            [snapshot.item_key for snapshot in snapshots],
        )
        displays = self._resolve(session, viewer, spec, snapshots)
        return [
            item
            for snapshot in snapshots
            if (display := displays.get(snapshot.item_key)) is not None
            and _matches(
                item := _item(spec, snapshot, marks.get(snapshot.item_key), display),
                state,
            )
        ]

    def _resolve_rows(
        self, session: Session, viewer: InboxViewer, rows: Sequence[InboxEntryRow]
    ) -> dict[tuple[str, str], tuple[InboxSnapshot, InboxDisplay]]:
        """Each row's current item and display, one source call per kind."""

        by_kind: dict[str, dict[str, InboxSnapshot | None]] = {}
        for row in rows:
            by_kind.setdefault(row.kind, {})[row.item_key] = entries.stored_snapshot(
                row
            )
        resolved: dict[tuple[str, str], tuple[InboxSnapshot, InboxDisplay]] = {}
        for name, stored in by_kind.items():
            spec = self.registry.kind(viewer.scope.name, name)
            found = spec.source.current(session, viewer, name, stored)
            displays = self._resolve(session, viewer, spec, list(found.values()))
            for item_key, snapshot in found.items():
                if (display := displays.get(item_key)) is not None:
                    resolved[(name, item_key)] = (snapshot, display)
        return resolved

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


def _position(at: datetime | None, kind: str, item_key: str) -> InboxPosition:
    moment = at or datetime.min.replace(tzinfo=UTC)
    moment = (
        moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment.astimezone(UTC)
    )
    return moment, kind, item_key


def _cursor_scope(viewer: InboxViewer) -> dict[str, Any]:
    return {
        "name": viewer.scope.name,
        "owner": {
            key: str(value) for key, value in viewer.scope.owner(viewer.user_id).items()
        },
    }


def _encode_cursor(
    position: InboxPosition,
    viewer: InboxViewer,
    state: InboxState,
    item_type: str,
) -> str:
    at, kind, item_key = position
    return encode_cursor(
        {
            "v": 2,
            "scope": _cursor_scope(viewer),
            "state": state,
            "item_type": item_type,
            "after": [at.isoformat(), kind, item_key],
        }
    )


def _decode_cursor(
    cursor: str, viewer: InboxViewer, state: InboxState, item_type: str
) -> InboxPosition:
    payload = decode_cursor(cursor)
    # Released stored-message cursors had only this three-element position.
    if isinstance(payload, list):
        return parse_position(payload)
    if (
        not isinstance(payload, dict)
        or type(payload.get("v")) is not int
        or payload.get("v") != 2
        or payload.get("scope") != _cursor_scope(viewer)
        or payload.get("state") != state
        or payload.get("item_type") != item_type
    ):
        raise ReadValidationError("invalid inbox cursor")
    return parse_position(payload.get("after"))

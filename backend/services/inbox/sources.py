"""Where a kind's items come from: written by producers, or derived on read.

A kind that should appear in the message stream materializes its items with a
``StoredSource``. A kind whose items are current state (a pending queue, a key
about to expire) derives them on read with a ``ComputedSource`` and shows in
the status group. A new source type implements ``WritableSource`` or
``StatusSource``; the registry routes kinds by that capability.
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.orm import Session

from backend.services.inbox import entries
from backend.services.inbox.base import (
    ComputedScan,
    InboxScope,
    InboxSnapshot,
    InboxViewer,
    StatusSource,
    WritableSource,
)


class StoredSource(WritableSource):
    """Items a producer writes in the same transaction as its source change.

    The write is a plain upsert: no commit, no lock beyond the rows it writes.
    InboxService first locks eligible recipients under the scope's lifecycle
    rule. The producer holds its own source lock and passes the latest state,
    which orders concurrent writes. Marks are kept; a new revision is unread
    because the saved read revision no longer matches.
    """

    def write(
        self,
        session: Session,
        scope: InboxScope,
        kind: str,
        recipient_ids: Sequence[UUID],
        snapshot: InboxSnapshot,
    ) -> None:
        entries.upsert_stored(session, scope, kind, recipient_ids, snapshot)

    def current(
        self,
        session: Session,
        viewer: InboxViewer,
        kind: str,
        item_key: str,
        stored: InboxSnapshot | None,
    ) -> InboxSnapshot | None:
        return stored


STORED = StoredSource()


@dataclass(frozen=True)
class ComputedSource(StatusSource):
    """Items derived on read from data the kind already owns.

    ``scan`` returns at most ``limit`` items plus the full total; a source with
    more items aggregates them or lets the overflow show. ``get`` returns one
    item by key, or None when it no longer exists for this viewer.
    """

    scan: Callable[[Session, InboxViewer, int], ComputedScan]
    get: Callable[[Session, InboxViewer, str], InboxSnapshot | None]

    def status(
        self, session: Session, viewer: InboxViewer, kind: str, limit: int
    ) -> ComputedScan:
        return self.scan(session, viewer, limit)

    def current(
        self,
        session: Session,
        viewer: InboxViewer,
        kind: str,
        item_key: str,
        stored: InboxSnapshot | None,
    ) -> InboxSnapshot | None:
        return self.get(session, viewer, item_key)

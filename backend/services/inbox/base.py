"""Inbox contracts: the three extension points and the values they exchange.

The service depends only on these abstractions:

* ``InboxScope`` -- where an inbox lives (a workspace, the account). It names
  the table, the owner key, the write lock and the link rule.
* ``InboxSource`` -- where a kind's items come from. ``WritableSource``
  items are written by producers and list as messages; ``StatusSource``
  items are derived on read and list as current state.
* ``InboxKind`` -- one message type: its scope, its source, how it is shown
  to the current viewer, and its own policies.

``InboxListener`` lets a delivery channel react to written items. Adding a
kind, a source, a scope or a channel implements one of these and registers
it; no service code changes.
"""

from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, ClassVar, Literal
from uuid import UUID

from sqlalchemy.orm import Session

from backend.policy.auth import ActorContext

InboxState = Literal["all", "unread", "archived"]


@dataclass(frozen=True)
class InboxDisplay:
    """What a kind shows for one item to the current viewer."""

    title: str | None = None
    href: str | None = None
    params: Mapping[str, str | int | bool | None] = field(default_factory=dict)


@dataclass(frozen=True)
class InboxSnapshot:
    """An item's current state: the revision, when it happened, its identifiers."""

    item_key: str
    revision: str
    event_at: datetime | None
    ref: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class ComputedScan:
    items: Sequence[InboxSnapshot]
    # Every item the source has for this viewer, not just the ones returned.
    total: int


class InboxScope(ABC):
    """Where an inbox lives. Each scope has its own entry table."""

    name: ClassVar[str]
    model: ClassVar[type]

    @abstractmethod
    def owner(self, user_id: UUID) -> dict[str, UUID]:
        """Key columns that identify this user's inbox in ``model``."""

    @abstractmethod
    def acquire_write(self, session: Session) -> None:
        """Serialize writers before reading state that a write depends on."""

    def writable_recipients(
        self, session: Session, recipient_ids: tuple[UUID, ...]
    ) -> tuple[UUID, ...]:
        """Apply the scope's recipient lifecycle and hold any required locks.

        Producers acquire their source locks first; entry writes follow this
        gate. Scopes without recipient lifecycle state accept every recipient.
        """

        return recipient_ids

    def check_display(self, display: InboxDisplay) -> None:
        """Reject a display this scope cannot show, such as a foreign link.

        A scope without such a rule accepts every display.
        """

        return None


@dataclass(frozen=True)
class InboxViewer:
    """The authenticated recipient and the inbox they are looking at."""

    actor: ActorContext
    user_id: UUID
    scope: InboxScope
    role: str | None = None


class InboxSource(ABC):
    """Where a kind's items come from. Every source answers for one item."""

    @abstractmethod
    def current(
        self,
        session: Session,
        viewer: InboxViewer,
        kind: str,
        item_key: str,
        stored: InboxSnapshot | None,
    ) -> InboxSnapshot | None:
        """The item's state now, or None if it is gone. ``stored`` is what
        the inbox itself holds for the item, if anything."""


class WritableSource(InboxSource):
    """A source whose items producers write; they list as messages."""

    @abstractmethod
    def write(
        self,
        session: Session,
        scope: InboxScope,
        kind: str,
        recipient_ids: Sequence[UUID],
        snapshot: InboxSnapshot,
    ) -> None: ...


class StatusSource(InboxSource):
    """A source whose items stand as current state, listed whole on the
    first page instead of in the message stream."""

    @abstractmethod
    def status(
        self, session: Session, viewer: InboxViewer, kind: str, limit: int
    ) -> ComputedScan: ...


class InboxListener(ABC):
    """Hears every write of a stored item: a delivery channel such as mail
    or push. It is called once per ``put`` with recipients, including a put
    that only refreshes an existing item, and runs inside the producer's
    transaction. It records what to deliver (an outbox row keyed so that a
    repeated revision is not delivered twice) rather than delivering."""

    @abstractmethod
    def on_put(
        self,
        session: Session,
        scope: InboxScope,
        kind: str,
        recipient_ids: Sequence[UUID],
        snapshot: InboxSnapshot,
    ) -> None: ...


# Returns a display for each item the viewer may see; a missing key hides it.
InboxResolver = Callable[
    [Session, InboxViewer, Sequence[InboxSnapshot]], Mapping[str, InboxDisplay]
]


@dataclass(frozen=True)
class InboxKind:
    name: str
    scope: str
    source: InboxSource
    resolve: InboxResolver
    # Whether a new revision brings an archived item back. Policy per kind.
    reopen_on_new_revision: bool = False


@dataclass(frozen=True)
class InboxItem:
    kind: str
    item_key: str
    revision: str
    event_at: datetime | None
    unread: bool
    archived: bool
    # None when this server has no such kind: no content, no link.
    display: InboxDisplay | None


@dataclass(frozen=True)
class InboxOverflow:
    kind: str
    total: int


@dataclass(frozen=True)
class InboxPage:
    status_items: list[InboxItem]
    status_overflow: list[InboxOverflow]
    items: list[InboxItem]
    next_cursor: str | None


@dataclass(frozen=True)
class InboxMarkResult:
    applied: bool
    item: InboxItem

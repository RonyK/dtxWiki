"""Container-owned registry of Inbox kinds, with no process-wide state."""

from collections.abc import Iterable, Mapping, Sequence
from types import MappingProxyType

from sqlalchemy.orm import Session

from backend.services.inbox.base import (
    InboxDisplay,
    InboxKind,
    InboxSnapshot,
    InboxViewer,
    StatusSource,
    WritableSource,
)
from backend.services.inbox.sources import STORED

# Shown in place of a kind this server no longer has: no content, no link.
UNREGISTERED_DISPLAY = InboxDisplay()


def _unregistered_resolve(
    _session: Session, _viewer: InboxViewer, snapshots: Sequence[InboxSnapshot]
) -> Mapping[str, InboxDisplay]:
    return {snapshot.item_key: UNREGISTERED_DISPLAY for snapshot in snapshots}


def _hidden_resolve(
    _session: Session, _viewer: InboxViewer, _snapshots: Sequence[InboxSnapshot]
) -> Mapping[str, InboxDisplay]:
    return {}


class InboxRegistry:
    """Kinds by scope, with each source's capability resolved once here so the
    service never inspects a source type."""

    def __init__(self, kinds: Iterable[InboxKind]) -> None:
        by_scope: dict[str, dict[str, InboxKind]] = {}
        names: set[str] = set()
        for kind in kinds:
            if kind.name in names:
                raise ValueError(f"duplicate inbox kind {kind.name!r}")
            names.add(kind.name)
            by_scope.setdefault(kind.scope, {})[kind.name] = kind
        self._by_scope = {
            scope: MappingProxyType(found) for scope, found in by_scope.items()
        }
        self._names = frozenset(names)
        self._status = {
            scope: tuple(
                kind for kind in found.values() if isinstance(kind.source, StatusSource)
            )
            for scope, found in by_scope.items()
        }

    def in_scope(self, scope: str) -> Mapping[str, InboxKind]:
        return self._by_scope.get(scope, MappingProxyType({}))

    def status_kinds(self, scope: str) -> tuple[InboxKind, ...]:
        """Kinds whose items list as current state on the first page."""

        return self._status.get(scope, ())

    def kind(self, scope: str, name: str) -> InboxKind:
        """The registered kind, or a stand-in for a name this scope does not
        have: rows left behind by a kind whose code was removed still list
        and mark without content, while a kind that belongs to another scope
        is never visible here."""

        registered = self.in_scope(scope).get(name)
        if registered is not None:
            return registered
        resolve = _hidden_resolve if name in self._names else _unregistered_resolve
        return InboxKind(name, scope, STORED, resolve)

    def writable(self, scope: str, name: str) -> InboxKind:
        kind = self.in_scope(scope).get(name)
        if kind is None or not isinstance(kind.source, WritableSource):
            raise ValueError(f"{name!r} is not a writable {scope} inbox kind")
        return kind

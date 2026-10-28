"""Per-user Inbox: a policy-free core that kinds, sources and scopes plug into."""

from backend.services.inbox.base import (
    ComputedScan,
    InboxDisplay,
    InboxItem,
    InboxKind,
    InboxListener,
    InboxMarkResult,
    InboxPage,
    InboxScope,
    InboxSnapshot,
    InboxSource,
    InboxState,
    InboxViewer,
    StatusSource,
    WritableSource,
)
from backend.services.inbox.kinds import BUILTIN_INBOX_KINDS
from backend.services.inbox.registry import InboxRegistry
from backend.services.inbox.scopes import AccountInbox, WorkspaceInbox
from backend.services.inbox.service import InboxService
from backend.services.inbox.sources import STORED, ComputedSource, StoredSource

__all__ = [
    "BUILTIN_INBOX_KINDS",
    "STORED",
    "AccountInbox",
    "ComputedScan",
    "ComputedSource",
    "InboxDisplay",
    "InboxItem",
    "InboxKind",
    "InboxListener",
    "InboxMarkResult",
    "InboxPage",
    "InboxRegistry",
    "InboxScope",
    "InboxService",
    "InboxSnapshot",
    "InboxSource",
    "InboxState",
    "InboxViewer",
    "StatusSource",
    "StoredSource",
    "WorkspaceInbox",
    "WritableSource",
]

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
from backend.services.inbox.config import (
    DEFAULT_INBOX_CONFIG,
    InboxConfig,
    InboxListingConfig,
)
from backend.services.inbox.kinds import build_builtin_inbox_kinds
from backend.services.inbox.registry import InboxRegistry
from backend.services.inbox.scopes import AccountInbox, WorkspaceInbox
from backend.services.inbox.service import InboxService
from backend.services.inbox.sources import STORED, ComputedSource, StoredSource

__all__ = [
    "DEFAULT_INBOX_CONFIG",
    "STORED",
    "AccountInbox",
    "ComputedScan",
    "ComputedSource",
    "InboxConfig",
    "InboxDisplay",
    "InboxItem",
    "InboxKind",
    "InboxListener",
    "InboxListingConfig",
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
    "build_builtin_inbox_kinds",
]

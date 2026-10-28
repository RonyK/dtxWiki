"""Inbox kinds built into the core distribution.

This is the composition point: a feature that adds a kind defines it in its
own module and lists it here. Overlays add theirs through
``ServiceRegistry.inbox_kinds``. Neither touches the service.
"""

from backend.services.inbox.base import InboxKind

BUILTIN_INBOX_KINDS: tuple[InboxKind, ...] = ()

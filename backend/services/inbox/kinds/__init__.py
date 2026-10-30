"""Inbox kinds built into the core distribution.

This is the composition point: a feature that adds a kind defines it in its
own module under this package and builds it here, handing it its section of
the central ``InboxConfig``. Overlays add theirs through
``ServiceRegistry.inbox_kinds``. Neither touches the service.
"""

from backend.services.inbox.base import InboxKind
from backend.services.inbox.config import InboxConfig


def build_builtin_inbox_kinds(config: InboxConfig) -> tuple[InboxKind, ...]:
    # Imported here so importing the Inbox package does not load every
    # feature a kind reads from.
    from backend.services.inbox.kinds.access_key import access_key_expiry_kind
    from backend.services.inbox.kinds.review import (
        review_attention_kind,
        review_result_kind,
    )
    from backend.services.inbox.kinds.workspace_join import workspace_join_kind

    return (
        review_attention_kind(),
        review_result_kind(),
        workspace_join_kind(config.workspace_join),
        access_key_expiry_kind(config.access_key_expiry),
    )

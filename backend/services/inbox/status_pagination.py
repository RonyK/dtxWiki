"""SQL boundaries for sources sharing the inbox's chronological keyset."""

from collections.abc import Callable
from typing import Any
from uuid import UUID

from sqlalchemy import and_, or_, true

from backend.services.inbox.base import InboxPosition
from backend.services.read_errors import ReadValidationError


def uuid_key(value: str) -> UUID:
    """Validate the canonical UUID spelling used by source item keys."""

    try:
        item_id = UUID(value)
        if str(item_id) != value:
            raise ValueError("noncanonical item key")
        return item_id
    except (ValueError, TypeError, AttributeError) as exc:
        raise ReadValidationError("invalid inbox cursor item key") from exc


def chronological_after(
    event_at: Any,
    kind: str,
    after: InboxPosition | None,
    key_before: Callable[[str], Any],
) -> Any:
    """Rows below a shared (event_at, kind, item_key) descending position."""

    if after is None:
        return true()
    moment, after_kind, item_key = after
    if kind < after_kind:
        return event_at <= moment
    if kind > after_kind:
        return event_at < moment
    return or_(
        event_at < moment,
        and_(event_at == moment, key_before(item_key)),
    )

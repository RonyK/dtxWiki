"""Shared encoding and text validation for Inbox continuation positions."""

import base64
import binascii
import json
from datetime import UTC, datetime
from typing import Any

from backend.services.inbox.base import InboxPosition
from backend.services.read_errors import ReadValidationError


def encode_cursor(payload: Any) -> str:
    raw = json.dumps(payload, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(cursor: str) -> Any:
    try:
        raw = base64.b64decode(
            cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True
        )
        return json.loads(raw)
    except (binascii.Error, ValueError, TypeError, UnicodeError, RecursionError) as exc:
        raise ReadValidationError("invalid inbox cursor") from exc


def parse_position(values: Any) -> InboxPosition:
    try:
        if not isinstance(values, list) or len(values) != 3:
            raise ValueError("invalid position")
        for value in values:
            if not isinstance(value, str) or not value or "\x00" in value:
                raise ValueError("invalid cursor text")
            value.encode("utf-8")
        if "T" not in values[0]:
            raise ValueError("invalid cursor time")
        moment = datetime.fromisoformat(values[0])
        moment = (
            moment.replace(tzinfo=UTC)
            if moment.tzinfo is None
            else moment.astimezone(UTC)
        )
        return moment, values[1], values[2]
    except (ValueError, TypeError, UnicodeError, OverflowError) as exc:
        raise ReadValidationError("invalid inbox cursor position") from exc

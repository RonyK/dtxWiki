"""Shared keyset-pagination cursor helpers.

The list endpoints (Context, Library, Agent, ...) order by
``(updated_at desc, id desc)``. The cursor carries the last row's
``updated_at`` + ``id`` so the next page can resume at a stable, gap-free
position. The token is opaque base64-JSON, and decoding is tolerant: a
missing or garbled cursor restarts at the first page (mirrors
``page_service`` / ``activity_service`` cursor handling) rather than raising.
"""

import base64
import binascii
import json
from datetime import datetime
from uuid import UUID

_CURSOR_VERSION = 1


def _encode_keyset_cursor(updated_at: datetime, item_id: UUID) -> str:
    payload = json.dumps(
        {"v": _CURSOR_VERSION, "ts": updated_at.isoformat(), "id": str(item_id)},
        separators=(",", ":"),
    ).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def _decode_keyset_cursor(cursor: str | None) -> tuple[datetime, UUID] | None:
    # Tolerant: a missing/garbled cursor restarts at the first page (returns
    # None) rather than raising, so a stale token never 500s the list.
    if not cursor:
        return None
    try:
        padding = "=" * (-len(cursor) % 4)
        raw = base64.urlsafe_b64decode(f"{cursor}{padding}").decode("utf-8")
        payload = json.loads(raw)
    except (binascii.Error, ValueError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or payload.get("v") != _CURSOR_VERSION:
        return None
    ts_raw = payload.get("ts")
    id_raw = payload.get("id")
    if not isinstance(ts_raw, str) or not isinstance(id_raw, str):
        return None
    try:
        return datetime.fromisoformat(ts_raw), UUID(id_raw)
    except ValueError:
        return None

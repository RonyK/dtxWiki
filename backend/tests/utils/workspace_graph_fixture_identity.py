"""Stable identities and timestamps shared by Workspace Graph fixtures."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid5

FIXTURE_NAMESPACE = UUID("9760ea6b-c37c-52c2-b8d2-2b086eac7890")
FIXED_TIME = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)


def fixture_uuid(corpus_key: str, *parts: object) -> UUID:
    """Return a stable identity scoped to one named fixture."""

    suffix = ":".join(str(part) for part in parts)
    return uuid5(FIXTURE_NAMESPACE, f"workspace-graph:{corpus_key}:{suffix}")


def fixture_time(offset_seconds: int = 0) -> datetime:
    """Return a stable actor-visible UTC timestamp."""

    return FIXED_TIME + timedelta(seconds=offset_seconds)

"""Narrow adapter for StreamableHTTP session-manager internals."""

from __future__ import annotations

from collections.abc import Container
from typing import Any


def manager_session_pool(manager: Any) -> Container[str] | None:
    """Return the SDK session-id pool when the pinned SDK exposes it.

    ``mcp`` 1.27.1 does not expose a public session lookup API. Keep the
    private attribute dependency isolated here so transport code does not
    spread SDK-shape assumptions across the route handler.
    """

    pool = getattr(manager, "_server_instances", None)
    if not isinstance(pool, Container):
        return None
    return pool


def manager_has_session(manager: Any, session_id: str) -> bool | None:
    pool = manager_session_pool(manager)
    if pool is None:
        return None
    return session_id in pool

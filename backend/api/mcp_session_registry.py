"""Keep the MCP SDK's stateful session pool bounded (0.1.1 contract C8).

``StreamableHTTPSessionManager`` registers a session for every request it is
handed without an ``Mcp-Session-Id`` -- before it knows whether that request
will initialize anything -- and the SDK releases this app supports keep a
session in the pool after a client DELETEs it. Only sessions bound to a bearer
(``app.state.mcp_session_owners``, capped) are usable. This module lets the
/mcp handler see which sessions a request opened, so the ones that never got
an owner can be discarded, and evict sessions once they are terminated.

Like backend/mcp/protocol/session_pool.py it depends on the manager's private
pool; that dependency stays in these two adapters and out of the route
handler.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

import anyio

from backend.mcp.protocol.session_pool import manager_session_pool

_OPENED_SESSION_IDS: ContextVar[list[str] | None] = ContextVar(
    "dtxwiki_mcp_opened_session_ids",
    default=None,
)


class _OpeningRecordingPool(dict):
    """The SDK session pool, reporting which ids the current request adds."""

    def __setitem__(self, key: str, value: Any) -> None:
        opened = _OPENED_SESSION_IDS.get()
        if opened is not None and key not in self:
            opened.append(key)
        super().__setitem__(key, value)


def record_session_openings(manager: Any) -> None:
    """Swap in a pool that reports openings; a no-op on an unknown SDK shape.

    The SDK adds a session to this pool from the task serving the request
    that opens it, so the context variable set around that request sees it.
    """

    pool = manager_session_pool(manager)
    if isinstance(pool, dict) and not isinstance(pool, _OpeningRecordingPool):
        manager._server_instances = _OpeningRecordingPool(pool)


@contextmanager
def sessions_opened_during_request() -> Iterator[list[str]]:
    opened: list[str] = []
    token = _OPENED_SESSION_IDS.set(opened)
    try:
        yield opened
    finally:
        _OPENED_SESSION_IDS.reset(token)


async def discard_sessions(manager: Any, session_ids: Iterable[str]) -> None:
    """Forget sessions and close their transports; idempotent.

    Shielded so a cancelled request (a client that went away) still releases
    the session it opened.
    """

    pool = manager_session_pool(manager)
    if not isinstance(pool, dict):
        return
    for session_id in session_ids:
        transport = pool.pop(session_id, None)
        if transport is not None and not getattr(transport, "is_terminated", True):
            with anyio.CancelScope(shield=True):
                await transport.terminate()


__all__ = [
    "discard_sessions",
    "record_session_openings",
    "sessions_opened_during_request",
]

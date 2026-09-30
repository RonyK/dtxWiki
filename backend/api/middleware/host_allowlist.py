"""Refuse requests whose Host header this deployment is not reached under.

A page on an attacker's domain can re-point that domain at 127.0.0.1 (DNS
rebinding) and then talk to a local dtxWiki as same-origin, cookies and all.
The browser still sends the attacker's host name in ``Host``, so answering
only the allowlisted names (backend/policy/allowed_hosts.py) closes the path
for the REST API, the SPA and /mcp alike, before any routing or
authentication happens.

The liveness and readiness probes stay reachable under any name: container
and load-balancer health checks address the app by IP or service name, and
those two endpoints expose nothing a rebinding page could use.
"""

from __future__ import annotations

import logging

from starlette.types import ASGIApp, Receive, Scope, Send
from starlette.websockets import WebSocketClose

from backend.api.middleware.request_body_limit import route_path
from backend.app.errors import build_rest_error_response
from backend.policy.allowed_hosts import host_is_allowed

logger = logging.getLogger("dtxwiki.security")

HEALTH_PROBE_PATHS = frozenset({"/health/live", "/health/ready"})
_HEALTH_PROBE_METHODS = frozenset({"GET", "HEAD"})
# Distinct refused hosts are logged once each, up to this many, so a
# misconfiguration is diagnosable without letting a flood fill the log.
_LOGGED_REFUSED_HOSTS_CAP = 64


class HostAllowlistMiddleware:
    def __init__(self, app: ASGIApp, *, allowed_hosts: frozenset[str] | None) -> None:
        self.app = app
        # None means DTXWIKI_ALLOWED_HOSTS=* (checked and warned about at
        # startup by Settings.validate_for_startup).
        self.allowed_hosts = allowed_hosts
        self._logged_refusals: set[str] = set()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            self.allowed_hosts is None
            or scope["type"] not in ("http", "websocket")
            or _is_health_probe(scope)
        ):
            await self.app(scope, receive, send)
            return
        host = _single_host_header(scope)
        if host is not None and host_is_allowed(host, self.allowed_hosts):
            await self.app(scope, receive, send)
            return
        self._log_refusal(host)
        if scope["type"] == "websocket":
            await WebSocketClose(code=1008)(scope, receive, send)
            return
        response = build_rest_error_response(
            "invalid_request",
            (
                "This server does not answer to the requested host name. If "
                "dtxWiki is reached under another name, add it to "
                "DTXWIKI_ALLOWED_HOSTS."
            ),
            status_code=400,
        )
        await response(scope, receive, send)

    def _log_refusal(self, host: str | None) -> None:
        key = (host or "")[:200]
        if key in self._logged_refusals:
            return
        if len(self._logged_refusals) >= _LOGGED_REFUSED_HOSTS_CAP:
            return
        self._logged_refusals.add(key)
        logger.warning(
            "refused a request for host %r: not in the allowed hosts "
            "(loopback, DTXWIKI_PUBLIC_BASE_URL, DTXWIKI_ALLOWED_HOSTS)",
            key,
        )


def _single_host_header(scope: Scope) -> str | None:
    values = [
        value for name, value in scope.get("headers", ()) if name.lower() == b"host"
    ]
    if len(values) != 1:
        return None
    try:
        return values[0].decode("ascii")
    except UnicodeDecodeError:
        return None


def _is_health_probe(scope: Scope) -> bool:
    if scope["type"] != "http" or scope.get("method") not in _HEALTH_PROBE_METHODS:
        return False
    return route_path(scope) in HEALTH_PROBE_PATHS


__all__ = ["HEALTH_PROBE_PATHS", "HostAllowlistMiddleware"]

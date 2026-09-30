"""HTTPS redirect that exempts plaintext health probes.

Production must be reached over HTTPS, but container/orchestrator liveness probes
hit the app port directly over HTTP (e.g. ``http://127.0.0.1:21080/health/live``).
A blanket ``HTTPSRedirectMiddleware`` answers those probes with a 307 redirect, so
the container never becomes healthy. Health endpoints expose no secrets, so they
are exempt; everything else is redirected exactly like Starlette's middleware.
"""

from __future__ import annotations

from starlette.datastructures import URL
from starlette.responses import RedirectResponse
from starlette.types import ASGIApp, Receive, Scope, Send

# Liveness/readiness probes that must answer over plain HTTP: the root
# router's /health/live and /health/ready.
HEALTH_EXEMPT_PREFIXES = ("/health/",)


class HTTPSRedirectExceptHealthMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] in ("http", "websocket") and scope["scheme"] in ("http", "ws"):
            path = scope.get("path", "")
            if not any(path.startswith(prefix) for prefix in HEALTH_EXEMPT_PREFIXES):
                url = URL(scope=scope)
                redirect_scheme = {"http": "https", "ws": "wss"}[url.scheme]
                netloc = url.hostname if url.port in (80, 443) else url.netloc
                url = url.replace(scheme=redirect_scheme, netloc=netloc)
                response = RedirectResponse(url, status_code=307)
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)

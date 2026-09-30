from __future__ import annotations

import json
import logging
import sys
import time
import uuid
from collections.abc import Awaitable, Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from backend.api.metrics import inc_http_request
from backend.policy.remote_ip import resolve_remote_ip

logger = logging.getLogger("dtxwiki.request")
if not logger.handlers:
    _stdout_buffer = getattr(sys.stdout, "buffer", None)
    if _stdout_buffer is not None:
        import io
        _stream = io.TextIOWrapper(
            _stdout_buffer, encoding="utf-8", errors="replace", line_buffering=True
        )
    else:
        _stream = sys.stdout
    handler = logging.StreamHandler(_stream)
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)
logger.setLevel(logging.INFO)
logger.propagate = False


class RequestLogMiddleware(BaseHTTPMiddleware):
    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        request_id = request.headers.get("x-request-id") or uuid.uuid4().hex
        request.state.request_id = request_id
        started = time.perf_counter()
        response: Response | None = None
        status = 500
        try:
            response = await call_next(request)
            status = response.status_code
            return response
        finally:
            latency_ms = int((time.perf_counter() - started) * 1000)
            inc_http_request(method=request.method, status=status)
            _emit_request_log(
                request,
                request_id=request_id,
                status=status,
                latency_ms=latency_ms,
            )
            if response is not None:
                response.headers.setdefault("x-request-id", request_id)


def _emit_request_log(
    request: Request,
    *,
    request_id: str,
    status: int,
    latency_ms: int,
) -> None:
    actor = getattr(request.state, "actor", None)
    record = {
        "request_id": request_id,
        "actor_kind": _actor_value(actor, "actor_kind", "kind"),
        "actor_id": _actor_value(actor, "actor_id", "id"),
        "client_id": _client_id(request, actor),
        "workspace_id": _workspace_id(request, actor),
        "method": request.method,
        "route": _route_template(request),
        "status": status,
        "latency_ms": latency_ms,
        "remote_ip": _remote_ip(request),
    }
    logger.info(json.dumps(record, ensure_ascii=False, separators=(",", ":")))


def _actor_value(actor, *names: str):
    if actor is None:
        return None
    for name in names:
        value = getattr(actor, name, None)
        if value is not None:
            return str(value)
    return None


def _workspace_id(request: Request, actor) -> str | None:
    value = getattr(request.state, "workspace_id", None)
    if value is None:
        value = getattr(actor, "workspace_id", None)
    return str(value) if value is not None else None


def _client_id(request: Request, actor) -> str | None:
    value = _actor_value(actor, "client_id")
    if value is None:
        value = getattr(request.state, "client_id", None)
    return str(value) if value else None


def _route_template(request: Request) -> str | None:
    route = request.scope.get("route")
    return getattr(route, "path", None) or request.url.path


def _remote_ip(request: Request) -> str | None:
    return resolve_remote_ip(request)

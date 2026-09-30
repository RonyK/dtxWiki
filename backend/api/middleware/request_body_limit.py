"""Bound every request body before routing and authentication.

FastAPI parses a route's body (JSON, urlencoded or multipart, spooling files
to disk) before it resolves dependencies, so authentication runs only after
the whole body has been read. Without a bound an anonymous client could make
the server buffer hundreds of megabytes, or stall the event loop parsing a
huge form, with one request. This middleware runs before routing:

* a declared ``Content-Length`` above the bound is answered with 413 without
  reading a byte;
* a body without one (chunked) is counted as it streams in, and reading stops
  at the first chunk that crosses the bound.

The bound is chosen per request from its method and path. Routes that accept
larger bodies declare their own bound (``request_body_limit`` on the endpoint,
or ``request_body_limit()`` on a route class), the unauthenticated auth and
OAuth form endpoints get a small one, /mcp is left to its own bounded reader,
and everything else gets ``DTXWIKI_MAX_REQUEST_BODY_BYTES``.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, TypeVar

from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import BaseRoute
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from backend.app.errors import build_rest_error_response
from backend.app.settings import Settings

# /auth and /oauth bodies are a few form fields or a small JSON object; a
# tight bound keeps their synchronous parsing (parse_qs, json.loads) from
# stalling the event loop on an oversized anonymous request.
AUTH_REQUEST_BODY_LIMIT_BYTES = 64 * 1024
AUTH_PATH_PREFIXES = ("/oauth/", "/api/v1/auth/")
# /mcp authenticates from headers before it reads anything and then reads
# through its own bounded reader (backend/api/mcp_http.py), which answers
# oversize bodies with a JSON-RPC error rather than a REST envelope.
SELF_BOUNDED_PATHS = frozenset({"/mcp"})
REQUEST_BODY_LIMIT_ATTRIBUTE = "__dtxwiki_request_body_limit__"

_Endpoint = TypeVar("_Endpoint", bound=Callable[..., Any])


@dataclass(frozen=True)
class BodyLimit:
    limit_bytes: int
    error_code: str
    message: str
    extra: Mapping[str, Any] | None = field(default=None)

    def response(self) -> Response:
        return build_rest_error_response(
            self.error_code,
            self.message,
            status_code=413,
            extra=dict(self.extra) if self.extra else None,
        )


def generic_body_limit(limit_bytes: int) -> BodyLimit:
    return BodyLimit(
        limit_bytes=limit_bytes,
        error_code="request_too_large",
        message=f"Request body exceeds the {limit_bytes} byte limit.",
    )


class RequestBodyTooLarge(StarletteHTTPException):
    """Raised from ``receive`` once a streamed body crosses its bound.

    An HTTPException subclass so FastAPI's body parsing re-raises it instead
    of turning it into a 400 "error parsing the body"; the app maps it back
    to its 413 response through :func:`request_body_too_large_handler`.
    """

    def __init__(self, limit: BodyLimit) -> None:
        super().__init__(
            status_code=413,
            detail={"error_code": limit.error_code, "message": limit.message},
        )
        self.limit = limit


async def request_body_too_large_handler(
    _request: Request,
    exc: Exception,
) -> Response:
    assert isinstance(exc, RequestBodyTooLarge)
    return exc.limit.response()


def request_body_limit(
    resolver: Callable[[Settings], BodyLimit],
) -> Callable[[_Endpoint], _Endpoint]:
    """Give one endpoint its own body bound, derived from Settings."""

    def decorate(endpoint: _Endpoint) -> _Endpoint:
        setattr(endpoint, REQUEST_BODY_LIMIT_ATTRIBUTE, resolver)
        return endpoint

    return decorate


@dataclass(frozen=True)
class _RouteRule:
    methods: frozenset[str]
    route: BaseRoute
    limit: BodyLimit

    def matches(self, method: str, path: str) -> bool:
        path_regex = getattr(self.route, "path_regex", None)
        return (
            method in self.methods
            and path_regex is not None
            and path_regex.match(path) is not None
        )


class RequestBodyLimits:
    """Choose the bound for a request from its method and path.

    The route table is read on first use rather than at construction: the
    middleware is registered before the routers are included.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        routes: Callable[[], Iterable[BaseRoute]],
    ) -> None:
        self._settings = settings
        self._routes = routes
        self._route_rules: tuple[_RouteRule, ...] | None = None
        self.default = generic_body_limit(settings.max_request_body_bytes)
        self.auth = generic_body_limit(
            min(AUTH_REQUEST_BODY_LIMIT_BYTES, settings.max_request_body_bytes)
        )

    def for_scope(self, scope: Scope) -> BodyLimit | None:
        path = route_path(scope)
        if path in SELF_BOUNDED_PATHS:
            return None
        method = str(scope.get("method") or "")
        for rule in self._rules():
            if rule.matches(method, path):
                return rule.limit
        if path.startswith(AUTH_PATH_PREFIXES):
            return self.auth
        return self.default

    def _rules(self) -> tuple[_RouteRule, ...]:
        if self._route_rules is None:
            self._route_rules = tuple(self._collect_rules())
        return self._route_rules

    def _collect_rules(self) -> Iterable[_RouteRule]:
        for route in self._routes():
            methods = getattr(route, "methods", None)
            if not methods:
                continue
            limit: BodyLimit | None = None
            resolver = getattr(
                getattr(route, "endpoint", None), REQUEST_BODY_LIMIT_ATTRIBUTE, None
            )
            if resolver is not None:
                limit = resolver(self._settings)
            else:
                route_limit = getattr(route, "request_body_limit", None)
                if callable(route_limit):
                    limit = route_limit()
            if limit is not None:
                yield _RouteRule(methods=frozenset(methods), route=route, limit=limit)


class RequestBodyLimitMiddleware:
    def __init__(self, app: ASGIApp, *, limits: RequestBodyLimits) -> None:
        self.app = app
        self.limits = limits

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        limit = self.limits.for_scope(scope)
        if limit is None:
            await self.app(scope, receive, send)
            return

        declared_length, invalid_length = declared_content_length(scope)
        if invalid_length:
            response = build_rest_error_response(
                "validation_failed",
                "Content-Length must be one non-negative decimal value.",
                status_code=400,
            )
            await response(scope, receive, send)
            return
        if declared_length is not None and declared_length > limit.limit_bytes:
            await limit.response()(scope, receive, send)
            return

        received_bytes = 0
        exceeded = False
        response_started = False

        async def bounded_receive() -> Message:
            nonlocal received_bytes, exceeded
            if exceeded:
                raise RequestBodyTooLarge(limit)
            message = await receive()
            if message["type"] == "http.request":
                received_bytes += len(message.get("body", b""))
                if received_bytes > limit.limit_bytes:
                    # Stop here: the rest of the stream is never read.
                    exceeded = True
                    raise RequestBodyTooLarge(limit)
            return message

        async def tracking_send(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, bounded_receive, tracking_send)
        except RequestBodyTooLarge:
            if response_started:
                raise
            await limit.response()(scope, receive, send)


def declared_content_length(scope: Scope) -> tuple[int | None, bool]:
    """Return ``(length, invalid)`` for the request's Content-Length header."""

    values = [
        value.strip()
        for name, value in scope.get("headers", ())
        if name.lower() == b"content-length"
    ]
    if not values:
        return None, False
    if len(values) != 1:
        return None, True
    try:
        value = values[0].decode("ascii")
    except UnicodeDecodeError:
        return None, True
    if not value.isdecimal() or len(value) > 20:
        return None, True
    return int(value), False


def route_path(scope: Scope) -> str:
    """The path the router matches on (``root_path`` removed)."""

    path = scope.get("path") or ""
    root_path = scope.get("root_path") or ""
    if root_path and path.startswith(root_path):
        return path[len(root_path) :] or "/"
    return path


__all__ = [
    "AUTH_REQUEST_BODY_LIMIT_BYTES",
    "BodyLimit",
    "RequestBodyLimitMiddleware",
    "RequestBodyLimits",
    "RequestBodyTooLarge",
    "declared_content_length",
    "generic_body_limit",
    "request_body_limit",
    "request_body_too_large_handler",
    "route_path",
]

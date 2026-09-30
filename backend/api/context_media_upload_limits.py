"""Raw request limits for the canonical Context Media upload route.

FastAPI resolves ``UploadFile`` by parsing and potentially spooling multipart
content before the endpoint function runs.  The custom route below therefore
buffers only this request body through a bounded ASGI ``receive`` loop,
validates the multipart envelope without materialising form fields, and then
replays the already-bounded body to FastAPI.

The incoming stream is consumed one message at a time into a spooled temporary
file, retaining ASGI backpressure while avoiding an unbounded in-memory body.
All other routes pass their original ``receive`` callable through unchanged.

The same byte bound is declared to the app-wide request-body middleware
(``request_body_limit``), which applies it before routing: the global
DTXWIKI_MAX_REQUEST_BODY_BYTES ceiling neither cuts this route short nor
widens it.
"""

from __future__ import annotations

from functools import partial
from tempfile import SpooledTemporaryFile
from typing import BinaryIO

import anyio
from fastapi.routing import APIRoute
from python_multipart.exceptions import FormParserError, MultipartParseError
from python_multipart.multipart import MultipartParser, parse_options_header
from starlette.requests import ClientDisconnect
from starlette.responses import Response
from starlette.types import Receive, Scope, Send

from backend.api.middleware.request_body_limit import (
    BodyLimit,
    declared_content_length,
)
from backend.app.errors import build_rest_error_response
from backend.context_media.domain.policy import (
    CONTEXT_MEDIA_UPLOAD_POLICY as _CONTEXT_MEDIA_UPLOAD_POLICY,
)
from backend.context_media.domain.policy import (
    ContextMediaUploadPolicy as _ContextMediaUploadPolicy,
)

_UPLOAD_ROUTE_SUFFIXES = ("/context/pages/{page_id}/media",)
_REPLAY_CHUNK_BYTES = 256 * 1024


class _MultipartResourceLimitExceeded(ValueError):
    pass


class _ReplayableBody:
    def __init__(self, spool: BinaryIO, byte_count: int) -> None:
        self._spool = spool
        self._byte_count = byte_count
        self._sent_bytes = 0
        self._finished = False

    async def receive(self) -> dict[str, object]:
        if self._finished:
            return {"type": "http.request", "body": b"", "more_body": False}
        chunk = await anyio.to_thread.run_sync(
            self._spool.read,
            min(_REPLAY_CHUNK_BYTES, self._byte_count - self._sent_bytes),
        )
        self._sent_bytes += len(chunk)
        self._finished = self._sent_bytes >= self._byte_count
        return {
            "type": "http.request",
            "body": chunk,
            "more_body": not self._finished,
        }

    async def close(self) -> None:
        await anyio.to_thread.run_sync(self._spool.close)


class ContextMediaUploadRoute(APIRoute):
    """Apply a pre-multipart boundary only to canonical Context uploads."""

    async def handle(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
    ) -> None:
        if not self._is_bounded_upload(scope):
            await super().handle(scope, receive, send)
            return

        body, rejection = await _read_bounded_upload_body(
            scope,
            receive,
            policy=_CONTEXT_MEDIA_UPLOAD_POLICY,
        )
        if rejection is not None:
            await rejection(scope, receive, send)
            return
        assert body is not None
        try:
            await super().handle(scope, body.receive, send)
        finally:
            await body.close()

    def _is_bounded_upload(self, scope: Scope) -> bool:
        return scope.get("method") == "POST" and self._is_upload_route()

    def _is_upload_route(self) -> bool:
        return any(self.path.endswith(suffix) for suffix in _UPLOAD_ROUTE_SUFFIXES)

    def request_body_limit(self) -> BodyLimit | None:
        """The pre-routing body bound for this route (see module docstring)."""

        if "POST" not in (self.methods or ()) or not self._is_upload_route():
            return None
        return context_media_body_limit(_CONTEXT_MEDIA_UPLOAD_POLICY)


def context_media_body_limit(policy: _ContextMediaUploadPolicy) -> BodyLimit:
    return BodyLimit(
        limit_bytes=policy.max_request_body_bytes,
        error_code="media_too_large",
        message=(
            "Context media encoded request body exceeds the "
            f"{policy.max_request_body_bytes} byte limit."
        ),
    )


async def _read_bounded_upload_body(
    scope: Scope,
    receive: Receive,
    *,
    policy: _ContextMediaUploadPolicy,
) -> tuple[_ReplayableBody | None, Response | None]:
    declared_length, invalid_length = declared_content_length(scope)
    if invalid_length:
        return None, build_rest_error_response(
            "validation_failed",
            "Content-Length must be one non-negative decimal value.",
            status_code=400,
        )
    if declared_length is not None and declared_length > policy.max_request_body_bytes:
        return None, _body_too_large_response(policy)

    content_type, invalid_content_type = _single_header(scope, b"content-type")
    if invalid_content_type:
        return None, build_rest_error_response(
            "validation_failed",
            "Content-Type must be specified at most once.",
            status_code=400,
        )

    spool: BinaryIO = SpooledTemporaryFile(
        max_size=policy.spool_memory_bytes,
        mode="w+b",
    )
    byte_count = 0
    try:
        while True:
            message = await receive()
            message_type = message.get("type")
            if message_type == "http.disconnect":
                raise ClientDisconnect
            if message_type != "http.request":
                continue
            chunk = message.get("body", b"")
            if not isinstance(chunk, bytes):
                raise RuntimeError("ASGI request body chunks must be bytes")
            byte_count += len(chunk)
            if byte_count > policy.max_request_body_bytes:
                spool.close()
                return None, _body_too_large_response(policy)
            if chunk:
                await anyio.to_thread.run_sync(spool.write, chunk)
            if not message.get("more_body", False):
                break

        await anyio.to_thread.run_sync(spool.seek, 0)
        multipart_rejection = await anyio.to_thread.run_sync(
            partial(
                _validate_multipart_envelope,
                content_type,
                spool,
                byte_count=byte_count,
                policy=policy,
            )
        )
        if multipart_rejection is not None:
            spool.close()
            return None, multipart_rejection
        await anyio.to_thread.run_sync(spool.seek, 0)
        return _ReplayableBody(spool, byte_count), None
    except BaseException:
        spool.close()
        raise


def _validate_multipart_envelope(
    content_type: bytes | None,
    spool: BinaryIO,
    *,
    byte_count: int,
    policy: _ContextMediaUploadPolicy,
) -> Response | None:
    if byte_count == 0:
        return None
    if content_type is None:
        return None
    media_type, options = parse_options_header(content_type)
    if media_type.lower() != b"multipart/form-data":
        return None
    boundary = options.get(b"boundary")
    if not boundary:
        # Preserve FastAPI's canonical malformed-form response.
        return None

    part_count = 0
    part_bytes = 0
    parse_completed = False

    def on_part_begin() -> None:
        nonlocal part_count, part_bytes
        part_count += 1
        part_bytes = 0
        if part_count > policy.max_multipart_parts:
            raise _MultipartResourceLimitExceeded("multipart part count exceeded")

    def on_part_data(data: bytes, start: int, end: int) -> None:
        del data
        nonlocal part_bytes
        part_bytes += end - start
        if part_bytes > policy.max_upload_bytes:
            raise _MultipartResourceLimitExceeded("multipart part size exceeded")

    def on_end() -> None:
        nonlocal parse_completed
        parse_completed = True

    try:
        parser = MultipartParser(
            boundary,
            {
                "on_part_begin": on_part_begin,
                "on_part_data": on_part_data,
                "on_end": on_end,
            },
            max_size=byte_count,
            max_header_count=policy.max_headers_per_part,
            max_header_size=policy.max_header_line_bytes,
        )
        while chunk := spool.read(_REPLAY_CHUNK_BYTES):
            parser.write(chunk)
        parser.finalize()
    except _MultipartResourceLimitExceeded:
        return _multipart_limit_response()
    except (FormParserError, MultipartParseError) as exc:
        if "maximum" in str(exc).lower():
            return _multipart_limit_response()
        # The boundary layer owns resource caps, while FastAPI retains its
        # existing wire response for malformed multipart syntax.
        return None

    if not parse_completed:
        return None
    return None


def _single_header(scope: Scope, target: bytes) -> tuple[bytes | None, bool]:
    values = [
        value for name, value in scope.get("headers", ()) if name.lower() == target
    ]
    if len(values) > 1:
        return None, True
    return (values[0] if values else None), False


def _body_too_large_response(policy: _ContextMediaUploadPolicy) -> Response:
    return context_media_body_limit(policy).response()


def _multipart_limit_response() -> Response:
    return build_rest_error_response(
        "request_too_large",
        "Context media multipart structure exceeds the request limits.",
        status_code=413,
    )


__all__ = ["ContextMediaUploadRoute"]

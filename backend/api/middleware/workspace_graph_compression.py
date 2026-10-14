from __future__ import annotations

import gzip
import re

from starlette.concurrency import run_in_threadpool
from starlette.types import ASGIApp, Message, Receive, Scope, Send

_PATH_PREFIX = "/api/v1/workspaces/"
_PATH_SUFFIX = "/graph/overview/query"
_QUALITY_VALUE = re.compile(rb"(?:0(?:\.[0-9]{0,3})?|1(?:\.0{0,3})?)")
_BYTE_SENSITIVE_HEADERS = {
    b"content-encoding",
    b"content-range",
    b"etag",
    b"content-md5",
    b"digest",
    b"content-digest",
    b"repr-digest",
    b"content-disposition",
}


def _is_overview_query(scope: Scope) -> bool:
    if scope["type"] != "http" or scope.get("method") != "POST":
        return False
    path = scope.get("path", "")
    if not path.startswith(_PATH_PREFIX) or not path.endswith(_PATH_SUFFIX):
        return False
    workspace_id = path[len(_PATH_PREFIX) : -len(_PATH_SUFFIX)]
    return bool(workspace_id) and "/" not in workspace_id


def _encoding_quality(parts: list[bytes]) -> int:
    if len(parts) == 1:
        return 1000
    if len(parts) != 2:
        return 0
    name, separator, value = parts[1].strip().partition(b"=")
    value = value.strip()
    if (
        not separator
        or name.strip().lower() != b"q"
        or _QUALITY_VALUE.fullmatch(value) is None
    ):
        return 0
    if value.startswith(b"1"):
        return 1000
    return int(value.partition(b".")[2].ljust(3, b"0"))


def _accepts_gzip(headers: list[tuple[bytes, bytes]]) -> bool:
    qualities: dict[bytes, int] = {}
    for name, value in headers:
        if name.lower() != b"accept-encoding":
            continue
        for entry in value.split(b","):
            parts = entry.split(b";")
            encoding = parts[0].strip().lower()
            if encoding not in {b"gzip", b"*"}:
                continue
            quality = _encoding_quality(parts)
            # Conflicting duplicate entries cannot turn an explicit refusal
            # into permission, including when they occur in separate headers.
            qualities[encoding] = min(qualities.get(encoding, 1000), quality)
    return qualities.get(b"gzip", qualities.get(b"*", 0)) > 0


def _has_no_transform(headers: list[tuple[bytes, bytes]]) -> bool:
    return any(
        directive.strip().partition(b"=")[0].strip().lower() == b"no-transform"
        for name, value in headers
        if name.lower() == b"cache-control"
        for directive in value.split(b",")
    )


def _eligible_start(message: Message) -> bool:
    if message.get("status") != 200 or message.get("trailers", False):
        return False
    headers = message.get("headers", [])
    if any(name.lower() in _BYTE_SENSITIVE_HEADERS for name, _ in headers):
        return False
    content_types = [
        value.partition(b";")[0].strip().lower()
        for name, value in headers
        if name.lower() == b"content-type"
    ]
    return content_types == [b"application/json"] and not _has_no_transform(headers)


def _with_vary(headers: list[tuple[bytes, bytes]]) -> list[tuple[bytes, bytes]]:
    result = list(headers)
    vary_indices = [
        index for index, (name, _) in enumerate(result) if name.lower() == b"vary"
    ]
    if any(
        token.strip().lower() in {b"accept-encoding", b"*"}
        for index in vary_indices
        for token in result[index][1].split(b",")
    ):
        return result
    if vary_indices:
        index = vary_indices[0]
        name, value = result[index]
        result[index] = (
            name,
            value + b", Accept-Encoding" if value else b"Accept-Encoding",
        )
    else:
        result.append((b"vary", b"Accept-Encoding"))
    return result


def _with_gzip_length(
    headers: list[tuple[bytes, bytes]], length: int
) -> list[tuple[bytes, bytes]]:
    encoded_length = str(length).encode("ascii")
    result = [
        (name, encoded_length if name.lower() == b"content-length" else value)
        for name, value in headers
    ]
    if not any(name.lower() == b"content-length" for name, _ in result):
        result.append((b"content-length", encoded_length))
    result.append((b"content-encoding", b"gzip"))
    return result


class WorkspaceGraphCompressionMiddleware:
    """Negotiate gzip for serialized, non-streaming Map overview JSON only."""

    def __init__(self, app: ASGIApp, *, minimum_size: int = 1000) -> None:
        self.app = app
        self.minimum_size = minimum_size

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        request_headers = scope.get("headers", [])
        if (
            not _is_overview_query(scope)
            or _has_no_transform(request_headers)
            or any(
                name.lower() in {b"range", b"if-range"} for name, _ in request_headers
            )
        ):
            await self.app(scope, receive, send)
            return

        accepts_gzip = _accepts_gzip(request_headers)
        initial_start: Message | None = None
        passthrough = False

        async def send_response(message: Message) -> None:
            nonlocal initial_start, passthrough
            if passthrough:
                await send(message)
                return
            if message["type"] == "http.response.start":
                if _eligible_start(message):
                    initial_start = message
                else:
                    passthrough = True
                    await send(message)
                return
            if initial_start is None:
                await send(message)
                return

            start = initial_start
            initial_start = None
            passthrough = True
            # Do not collect an unknown stream, even when its first chunk is
            # large. Extensions such as pathsend also remain untouched.
            if message["type"] != "http.response.body" or message.get(
                "more_body", False
            ):
                await send(start)
                await send(message)
                return

            body = message.get("body", b"")
            if len(body) >= self.minimum_size:
                headers = _with_vary(start.get("headers", []))
                if accepts_gzip:
                    compressed = await run_in_threadpool(
                        gzip.compress, body, compresslevel=6, mtime=0
                    )
                    if len(compressed) < len(body):
                        headers = _with_gzip_length(headers, len(compressed))
                        message = {**message, "body": compressed}
                start = {**start, "headers": headers}
            await send(start)
            await send(message)

        await self.app(scope, receive, send_response)
        if initial_start is not None:
            await send(initial_start)

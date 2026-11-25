"""Mechanics shared by the built-in adapters: one JSON POST and its errors.

Adapters compose these helpers instead of inheriting from a provider base
class, so each module holds only its own wire format.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

from backend.spi.llm import (
    ExecutionContext,
    LLMAdapterDescriptor,
    LLMAdapterError,
    LLMConnection,
    LLMErrorCode,
    LLMTransport,
    Termination,
    TextGenerationPort,
)

NO_CONFIG_SCHEMA: Mapping[str, object] = {
    "type": "object",
    "properties": {},
    "additionalProperties": False,
}


def api_key_schema(*, required: bool) -> Mapping[str, object]:
    return {
        "type": "object",
        "properties": {
            "api_key": {"type": "string", "minLength": 1, "writeOnly": True}
        },
        "required": ["api_key"] if required else [],
        "additionalProperties": False,
    }


# HTTP semantics every provider shares. A provider's error body may refine
# the code (for example an exhausted quota reported as 429).
_STATUS_ERROR_CODES: Mapping[int, LLMErrorCode] = {
    400: "invalid_request",
    401: "authentication_failed",
    402: "quota_exhausted",
    403: "permission_denied",
    404: "not_found",
    408: "timeout",
    413: "invalid_request",
    422: "invalid_request",
    429: "rate_limited",
    504: "timeout",
}

ErrorRefinement = Callable[[Mapping[str, Any]], LLMErrorCode | None]
_MAX_EVENT_BYTES = 1024 * 1024
_MAX_STREAM_BYTES = 8 * _MAX_EVENT_BYTES
_LINE_END = re.compile(rb"\r\n?|\n")


@dataclass(frozen=True)
class BuiltinAdapterFactory:
    descriptor: LLMAdapterDescriptor
    build: Callable[[LLMConnection, LLMTransport], TextGenerationPort]

    def create(
        self, connection: LLMConnection, transport: LLMTransport
    ) -> TextGenerationPort:
        return self.build(connection, transport)


@dataclass(frozen=True)
class JsonReply:
    body: Mapping[str, Any] = field(repr=False)
    request_id: str | None = None


async def post_json_object(
    transport: LLMTransport,
    url: str,
    *,
    headers: Mapping[str, str],
    payload: Mapping[str, object],
    context: ExecutionContext,
    request_id_header: str | None = None,
    refine_error: ErrorRefinement | None = None,
) -> JsonReply:
    """POST once and return the decoded JSON object of a 2xx reply.

    Any HTTP reply proves the provider received the request, so every error
    raised here has delivery ``sent``.
    """

    response = await transport.post_json(
        url,
        headers=headers,
        payload=payload,
        timeout_seconds=context.timeout_seconds,
    )
    request_id = response.headers.get(request_id_header) if request_id_header else None
    body = _json_object(response.body)
    if not 200 <= response.status_code < 300:
        error = body.get("error") if body is not None else None
        raise LLMAdapterError(
            _error_code(
                response.status_code,
                error if isinstance(error, Mapping) else {},
                refine_error,
            ),
            delivery="sent",
            status_code=response.status_code,
            retry_after_seconds=_retry_after(response.headers.get("retry-after")),
            provider_request_id=request_id,
        )
    if body is None:
        raise LLMAdapterError(
            "invalid_response",
            delivery="sent",
            status_code=response.status_code,
            provider_request_id=request_id,
        )
    return JsonReply(body=body, request_id=request_id)


@asynccontextmanager
async def open_json_events(
    transport: LLMTransport,
    url: str,
    *,
    headers: Mapping[str, str],
    payload: Mapping[str, object],
    context: ExecutionContext,
    request_id_header: str | None = None,
    refine_error: ErrorRefinement | None = None,
) -> AsyncIterator[tuple[AsyncIterator[dict[str, Any] | None], str | None]]:
    """Request an SSE response. ``None`` denotes the Chat Completions terminator."""

    async with transport.open_sse(
        url, headers=headers, payload=payload, timeout_seconds=context.timeout_seconds
    ) as response:
        request_id = (
            response.headers.get(request_id_header) if request_id_header else None
        )
        if not response.is_success:
            raw = bytearray()
            async for block in response.aiter_bytes():
                raw.extend(block[: _MAX_EVENT_BYTES - len(raw)])
                if len(raw) >= _MAX_EVENT_BYTES:
                    break
            body = _json_object(bytes(raw)) or {}
            raise LLMAdapterError(
                _error_code(
                    response.status_code, mapping(body.get("error")), refine_error
                ),
                delivery="sent",
                status_code=response.status_code,
                retry_after_seconds=_retry_after(response.headers.get("retry-after")),
                provider_request_id=request_id,
            )
        if (
            response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
            != "text/event-stream"
        ):
            raise _stream_error(request_id)
        yield _json_events(response.aiter_bytes(), request_id), request_id


async def _json_events(
    blocks: AsyncIterator[bytes],
    request_id: str | None,
) -> AsyncIterator[dict[str, Any] | None]:
    data: list[bytes] = []
    data_size = 0
    async for line in _event_lines(blocks, request_id):
        if not line and data:
            value = b"\n".join(data)
            data.clear()
            data_size = 0
            if value == b"[DONE]":
                yield None
            else:
                event = _json_object(value)
                if event is None:
                    raise _stream_error(request_id)
                yield event
        elif line.startswith(b"data:"):
            data.append(line[5:].removeprefix(b" "))
            data_size += len(data[-1]) + 1
            if data_size > _MAX_EVENT_BYTES:
                raise _stream_error(request_id)


async def _event_lines(
    blocks: AsyncIterator[bytes],
    request_id: str | None,
) -> AsyncIterator[bytes]:
    pending = b""
    received = 0
    first_line = True
    async for block in blocks:
        received += len(block)
        if received > _MAX_STREAM_BYTES:
            raise _stream_error(request_id)
        raw = pending + block
        # Keep a trailing CR until the following read distinguishes CR/LF.
        cr = b"\r" if raw.endswith(b"\r") else b""
        lines = _LINE_END.split(raw[:-1] if cr else raw)
        pending = lines.pop() + cr
        if len(pending) > _MAX_EVENT_BYTES:
            raise _stream_error(request_id)
        for line in lines:
            if len(line) > _MAX_EVENT_BYTES:
                raise _stream_error(request_id)
            if first_line:
                first_line = False
                line = line.removeprefix(b"\xef\xbb\xbf")
            yield line
    if pending.endswith(b"\r"):
        yield pending[:-1]


def _stream_error(request_id: str | None = None) -> LLMAdapterError:
    return LLMAdapterError(
        "invalid_response", delivery="sent", provider_request_id=request_id
    )


def termination(
    reported: Termination, *, refused: bool = False, tool_request: bool = False
) -> Termination:
    """Refusal and filtering outrank any text; a tool request outranks the
    reported finish."""

    if refused:
        return "refused"
    if reported == "blocked":
        return "blocked"
    if tool_request:
        return "tool_request"
    return reported


def count(value: object) -> int | None:
    """A reported token count, or ``None`` when absent or malformed."""

    return value if type(value) is int and value >= 0 else None


def count_or_zero(container: Mapping[str, Any], key: str) -> int | None:
    """A count whose omission means zero; a malformed value stays unknown."""

    value = container.get(key)
    return 0 if value is None else count(value)


def total(*counts: int | None) -> int | None:
    """The sum of known counts; unknown when any part is unknown."""

    return None if any(part is None for part in counts) else sum(counts)


def mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def sequence(value: object) -> list[Any]:
    return value if isinstance(value, list) else []


def text_of(value: object) -> str:
    return value if isinstance(value, str) else ""


def _error_code(
    status_code: int,
    error: Mapping[str, Any],
    refine_error: ErrorRefinement | None,
) -> LLMErrorCode:
    refined = refine_error(error) if refine_error is not None else None
    if refined is not None:
        return refined
    if status_code in _STATUS_ERROR_CODES:
        return _STATUS_ERROR_CODES[status_code]
    if status_code >= 500:
        return "provider_unavailable"
    return "provider_error"


def _json_object(body: bytes) -> dict[str, Any] | None:
    try:
        decoded = json.loads(body)
    except ValueError:
        return None
    return decoded if isinstance(decoded, dict) else None


def _retry_after(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        seconds = float(value)
    except ValueError:
        return None
    return seconds if math.isfinite(seconds) and seconds >= 0 else None

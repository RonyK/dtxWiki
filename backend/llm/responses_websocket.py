"""A single OpenAI Responses WebSocket run with native mid-turn steering."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass, fields, replace
from typing import Any

from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import InvalidStatus, WebSocketException

from backend.llm.adapters._shared import _error_code, mapping, text_of, total
from backend.llm.adapters.openai_errors import refine_openai_error
from backend.spi.llm import (
    CanonicalTextResult,
    ExecutionContext,
    LLMAdapterError,
    NextSteer,
    OnDelta,
    OnSteer,
    SteerInput,
    SteerUpdate,
    TokenUsage,
)

_URL = "wss://api.openai.com/v1/responses"
_MESSAGE_LIMIT = 1024 * 1024
_RUN_LIMIT = 8 * _MESSAGE_LIMIT
_STEER_ERRORS = frozenset(
    {
        "invalid_input",
        "response_not_found",
        "steering_not_supported",
        "too_many_pending_steers",
        "response_already_completed",
        "response_not_active",
        "successor_creation_failed",
    }
)
_TERMINAL_EVENTS = ("response.completed", "response.incomplete", "response.failed")
# The library's DEBUG logs include handshake headers and message bodies.
_SOCKET_LOGGER = logging.Logger("dtxwiki.responses_websocket", logging.WARNING)


class _FixedEndpointConnect(connect):
    def process_redirect(self, exc: Exception) -> Exception:
        # A redirect could otherwise forward the stored Bearer credential.
        return exc


def _open_socket(api_key: str, timeout: float):
    return _FixedEndpointConnect(
        _URL,
        additional_headers={"Authorization": f"Bearer {api_key}"},
        open_timeout=min(10, timeout),
        close_timeout=min(5, timeout),
        max_size=_MESSAGE_LIMIT,
        logger=_SOCKET_LOGGER,
    )


@dataclass(slots=True)
class _Pending:
    request: SteerInput
    parent: str
    provider_id: str | None = None


class _Run:
    def __init__(
        self,
        timeout: float,
        response_result: Callable[[Mapping[str, Any], str | None], CanonicalTextResult],
        on_delta: OnDelta,
        next_steer: NextSteer,
        on_steer: OnSteer,
    ) -> None:
        self.timeout = timeout
        self.response_result = response_result
        self.on_delta = on_delta
        self.next_steer = next_steer
        self.on_steer = on_steer
        self.active_id: str | None = None
        self.pending: _Pending | None = None
        self.terminal: CanonicalTextResult | None = None
        self.usage: TokenUsage | None = None
        self.attempted_create = False
        self.provider_replied = False
        self.received_bytes = 0

    async def receive(
        self, socket: ClientConnection, payload: Mapping[str, object]
    ) -> CanonicalTextResult:
        incoming: asyncio.Task[str | bytes] | None = None
        steering: asyncio.Task[SteerInput] | None = None
        try:
            self.attempted_create = True
            await self._send(socket, {**payload, "type": "response.create"})
            while True:
                if incoming is None:
                    incoming = asyncio.create_task(self._recv(socket))
                if (
                    steering is None
                    and self.active_id is not None
                    and self.pending is None
                ):
                    steering = asyncio.create_task(self.next_steer())
                watching = (incoming, steering) if steering is not None else (incoming,)
                ready, _ = await asyncio.wait(
                    watching, return_when=asyncio.FIRST_COMPLETED
                )
                if incoming in ready:
                    raw = incoming.result()
                    incoming = None
                    await self._event(self._decode(raw))
                    if self.terminal is not None and self.pending is None:
                        return replace(
                            self.terminal, usage=self.usage or self.terminal.usage
                        )
                if steering is not None and steering in ready:
                    request = steering.result()
                    steering = None
                    if self.active_id is not None and self.pending is None:
                        await self._steer(socket, request)
        finally:
            tasks = tuple(task for task in (incoming, steering) if task is not None)
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            if self.pending is not None:
                # The application owns this input. A broken socket provides
                # no proof whether the provider applied it before disconnect.
                with suppress(Exception):
                    await self.on_steer(SteerUpdate(self.pending.request.id, "unknown"))

    async def _send(
        self, socket: ClientConnection, event: Mapping[str, object]
    ) -> None:
        await asyncio.wait_for(socket.send(json.dumps(dict(event))), self.timeout)

    async def _recv(self, socket: ClientConnection) -> str | bytes:
        return await asyncio.wait_for(socket.recv(), self.timeout)

    def _decode(self, raw: str | bytes) -> Mapping[str, Any]:
        self.provider_replied = True
        self.received_bytes += len(raw.encode("utf-8") if isinstance(raw, str) else raw)
        if self.received_bytes > _RUN_LIMIT:
            raise LLMAdapterError("invalid_response", delivery="sent")
        try:
            event = json.loads(raw)
        except (ValueError, RecursionError):
            raise LLMAdapterError("invalid_response", delivery="sent") from None
        if not isinstance(event, dict):
            raise LLMAdapterError("invalid_response", delivery="sent")
        return event

    async def _steer(self, socket: ClientConnection, request: SteerInput) -> None:
        assert self.active_id is not None
        await self.on_steer(SteerUpdate(request.id, "sent"))
        self.pending = _Pending(request, self.active_id)
        await self._send(
            socket,
            {
                "type": "response.steer",
                "previous_response_id": self.active_id,
                "input": request.text,
            },
        )

    async def _event(self, event: Mapping[str, Any]) -> None:
        kind = event.get("type")
        if kind == "response.created":
            response_id = text_of(mapping(event.get("response")).get("id"))
            if not response_id:
                raise LLMAdapterError("invalid_response", delivery="sent")
            if self.pending is not None and response_id != self.pending.parent:
                await self.on_steer(SteerUpdate(self.pending.request.id, "applied"))
                self.pending = None
            self.active_id = response_id
            self.terminal = None
        elif kind in ("response.steer.accepted", "response.steer.failed"):
            await self._steer_event(kind, event)
        elif kind == "response.steer.pending":
            # Ask sends no client tools or approval requests, so it cannot
            # produce the required input for this continuation.
            raise LLMAdapterError("invalid_response", delivery="sent")
        elif kind == "response.output_text.delta" and self.active_id is not None:
            piece = text_of(event.get("delta"))
            if piece:
                await self.on_delta(piece)
        elif kind in _TERMINAL_EVENTS:
            response = mapping(event.get("response"))
            if self.active_id is None or response.get("id") != self.active_id:
                raise LLMAdapterError("invalid_response", delivery="sent")
            # A parent's completed or steered response remains provisional
            # while an accepted update can create another response.
            self.terminal = self.response_result(response, self.active_id)
            self.active_id = None
            self.usage = _combine_usage(self.usage, self.terminal.usage)
        elif kind == "error":
            error = mapping(event.get("error")) or event
            raise LLMAdapterError(
                refine_openai_error(error) or "provider_error", delivery="sent"
            )

    async def _steer_event(self, kind: str, event: Mapping[str, Any]) -> None:
        pending = self.pending
        steer = mapping(event.get("steer"))
        if pending is None or steer.get("previous_response_id") != pending.parent:
            return
        provider_id = text_of(steer.get("id")) or None
        if pending.provider_id is not None and provider_id != pending.provider_id:
            return
        if kind == "response.steer.accepted":
            pending.provider_id = provider_id
            await self.on_steer(SteerUpdate(pending.request.id, "accepted"))
        else:
            code = text_of(mapping(event.get("error")).get("code"))
            await self.on_steer(
                SteerUpdate(
                    pending.request.id,
                    "failed",
                    code if code in _STEER_ERRORS else "provider_error",
                )
            )
            self.pending = None


def _combine_usage(before: TokenUsage | None, after: TokenUsage) -> TokenUsage:
    if before is None:
        return after
    return TokenUsage(
        **{
            field.name: total(getattr(before, field.name), getattr(after, field.name))
            for field in fields(TokenUsage)
        }
    )


async def stream_responses_websocket(
    payload: Mapping[str, object],
    api_key: str,
    context: ExecutionContext,
    *,
    response_result: Callable[[Mapping[str, Any], str | None], CanonicalTextResult],
    on_delta: OnDelta,
    next_steer: NextSteer,
    on_steer: OnSteer,
) -> CanonicalTextResult:
    run = _Run(context.timeout_seconds, response_result, on_delta, next_steer, on_steer)
    try:
        async with _open_socket(api_key, context.timeout_seconds) as socket:
            return await run.receive(socket, payload)
    except InvalidStatus as exc:
        status = exc.response.status_code
        raise LLMAdapterError(
            _error_code(status, {}, None), delivery="not_sent", status_code=status
        ) from None
    except (TimeoutError, OSError, WebSocketException) as exc:
        delivery = (
            "sent"
            if run.provider_replied
            else "unknown"
            if run.attempted_create
            else "not_sent"
        )
        raise LLMAdapterError(
            "timeout" if isinstance(exc, TimeoutError) else "network_error",
            delivery=delivery,
        ) from None

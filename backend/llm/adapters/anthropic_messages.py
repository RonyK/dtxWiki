"""Anthropic Messages API (``POST /v1/messages``)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from backend.llm.adapters._shared import (
    NO_CONFIG_SCHEMA,
    BuiltinAdapterFactory,
    _stream_error,
    api_key_schema,
    count,
    count_or_zero,
    mapping,
    open_json_events,
    post_json_object,
    sequence,
    text_of,
    total,
)
from backend.spi.llm import (
    CanonicalTextRequest,
    CanonicalTextResult,
    ExecutionContext,
    GenerationFeatures,
    LLMAdapterDescriptor,
    LLMAdapterError,
    LLMConnection,
    LLMErrorCode,
    LLMTransport,
    OnDelta,
    Termination,
    TokenUsage,
)

# The API version this implementation was written and verified against.
API_VERSION = "2023-06-01"

_TERMINATIONS: Mapping[str, Termination] = {
    "end_turn": "completed",
    "stop_sequence": "completed",
    "max_tokens": "truncated",
    "model_context_window_exceeded": "truncated",
    "refusal": "refused",
    "tool_use": "tool_request",
}

DESCRIPTOR = LLMAdapterDescriptor(
    adapter_id="builtin.anthropic.messages",
    adapter_version="1.0.0",
    display_name={"en": "Anthropic Messages API", "ko": "Anthropic Messages API"},
    config_schema=NO_CONFIG_SCHEMA,
    credential_schema=api_key_schema(required=True),
    features=GenerationFeatures(streaming=True, steering=False),
    options=frozenset({"temperature"}),
    default_base_url="https://api.anthropic.com/v1",
)


class AnthropicMessagesAdapter:
    features = DESCRIPTOR.features

    def __init__(
        self, connection: LLMConnection, transport: LLMTransport, _model: str
    ) -> None:
        self._connection = connection
        self._transport = transport

    async def generate(
        self, request: CanonicalTextRequest, context: ExecutionContext
    ) -> CanonicalTextResult:
        reply = await post_json_object(
            self._transport,
            f"{self._connection.base_url}/messages",
            headers={
                "x-api-key": self._connection.credential["api_key"],
                "anthropic-version": API_VERSION,
            },
            payload=_payload(request),
            context=context,
            request_id_header="request-id",
            refine_error=_refine_error,
        )
        body = reply.body
        return CanonicalTextResult(
            # Thinking and tool blocks are not answer text.
            text="".join(
                text_of(block.get("text"))
                for block in map(mapping, sequence(body.get("content")))
                if block.get("type") == "text"
            ),
            termination=_TERMINATIONS.get(text_of(body.get("stop_reason")), "unknown"),
            model=text_of(body.get("model")) or None,
            usage=_usage(mapping(body.get("usage"))),
            provider_request_id=reply.request_id,
        )

    async def stream(
        self,
        request: CanonicalTextRequest,
        context: ExecutionContext,
        *,
        on_delta: OnDelta,
    ) -> CanonicalTextResult:
        model = None
        reason = ""
        usage = {}
        text = []
        async with open_json_events(
            self._transport,
            f"{self._connection.base_url}/messages",
            headers={
                "x-api-key": self._connection.credential["api_key"],
                "anthropic-version": API_VERSION,
            },
            payload={**_payload(request), "stream": True},
            context=context,
            request_id_header="request-id",
            refine_error=_refine_error,
        ) as (events, request_id):
            async for event in events:
                if event is None:
                    raise _stream_error(request_id)
                kind = event.get("type")
                if kind == "message_start":
                    message = mapping(event.get("message"))
                    model = text_of(message.get("model")) or None
                    usage.update(mapping(message.get("usage")))
                elif kind == "message_delta":
                    reason = (
                        text_of(mapping(event.get("delta")).get("stop_reason"))
                        or reason
                    )
                    usage.update(mapping(event.get("usage")))
                elif kind == "message_stop":
                    return CanonicalTextResult(
                        text="".join(text),
                        termination=_TERMINATIONS.get(reason, "unknown"),
                        model=model,
                        usage=_usage(usage),
                        provider_request_id=request_id,
                    )
                elif kind == "error":
                    error = mapping(event.get("error"))
                    raise LLMAdapterError(
                        _refine_error(error)
                        or _STREAM_ERRORS.get(
                            text_of(error.get("type")), "provider_error"
                        ),
                        delivery="sent",
                        provider_request_id=request_id,
                    )
                else:
                    block = mapping(event.get("content_block"))
                    delta = mapping(event.get("delta"))
                    piece = ""
                    if kind == "content_block_start" and block.get("type") == "text":
                        piece = text_of(block.get("text"))
                    elif (
                        kind == "content_block_delta"
                        and delta.get("type") == "text_delta"
                    ):
                        piece = text_of(delta.get("text"))
                    if piece:
                        text.append(piece)
                        await on_delta(piece)
            raise _stream_error(request_id)


def _payload(request: CanonicalTextRequest) -> dict[str, object]:
    payload: dict[str, object] = {
        "model": request.model,
        "max_tokens": request.max_output_tokens,
        "messages": [
            {"role": message.role, "content": message.text}
            for message in request.messages
        ],
    }
    if request.system:
        payload["system"] = request.system
    if request.temperature is not None:
        payload["temperature"] = request.temperature
    return payload


def _usage(usage: Mapping[str, Any]) -> TokenUsage:
    # Anthropic reports uncached input and both cache counts separately; an
    # omitted cache count means no cache was used.
    return TokenUsage(
        input_tokens=total(
            count(usage.get("input_tokens")),
            count_or_zero(usage, "cache_read_input_tokens"),
            count_or_zero(usage, "cache_creation_input_tokens"),
        ),
        output_tokens=count(usage.get("output_tokens")),
        cache_read_input_tokens=count(usage.get("cache_read_input_tokens")),
        cache_write_input_tokens=count(usage.get("cache_creation_input_tokens")),
        reasoning_output_tokens=count(
            mapping(usage.get("output_tokens_details")).get("thinking_tokens")
        ),
    )


# The contract and usage-tier spend limits arrive as a 429 rate_limit_error;
# waiting does not lift them.
_SPEND_LIMIT_ERROR_CODES = frozenset(
    {"contract_spend_limit_reached", "enforced_spend_limit_reached"}
)
_STREAM_ERRORS: Mapping[str, LLMErrorCode] = {
    "overloaded_error": "provider_unavailable",
    "rate_limit_error": "rate_limited",
    "invalid_request_error": "invalid_request",
}


def _refine_error(error: Mapping[str, Any]) -> LLMErrorCode | None:
    if mapping(error.get("details")).get("error_code") in _SPEND_LIMIT_ERROR_CODES:
        return "quota_exhausted"
    return None


FACTORY = BuiltinAdapterFactory(descriptor=DESCRIPTOR, build=AnthropicMessagesAdapter)

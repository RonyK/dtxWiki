"""OpenAI Responses API (``POST /v1/responses``)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from backend.llm.adapters._shared import (
    NO_CONFIG_SCHEMA,
    BuiltinAdapterFactory,
    _stream_error,
    api_key_schema,
    count,
    mapping,
    open_json_events,
    post_json_object,
    sequence,
    termination,
    text_of,
)
from backend.llm.adapters.openai_errors import refine_openai_error
from backend.spi.llm import (
    CanonicalTextRequest,
    CanonicalTextResult,
    ExecutionContext,
    GenerationFeatures,
    LLMAdapterDescriptor,
    LLMAdapterError,
    LLMConnection,
    LLMTransport,
    NextSteer,
    OnDelta,
    OnSteer,
    Termination,
    TokenUsage,
)

_INCOMPLETE_TERMINATIONS: Mapping[str, Termination] = {
    "max_output_tokens": "truncated",
    "max_messages": "truncated",
    "content_filter": "blocked",
}
# The API refuses a smaller output budget; raising it would spend more than
# the caller allowed.
_MIN_OUTPUT_TOKENS = 16
# Calls the model asks the client to run. Hosted tools run at OpenAI and are
# not requests to dtxWiki.
_CLIENT_TOOL_CALLS = frozenset({"function_call", "custom_tool_call"})
_OPENAI_BASE_URL = "https://api.openai.com/v1"
# Published models in the GPT-6 family take instructions while they answer,
# over OpenAI's own WebSocket endpoint. Aliases outside this catalog and
# other servers stream without them.
_STEER_MODELS = frozenset(
    {
        "gpt-6-astra",
        "gpt-6-sol",
        "gpt-6-luna",
        "gpt-6.1-sol",
        "gpt-6.1-terra",
        "gpt-6.2-astra",
        "gpt-6.2-sol",
    }
)

DESCRIPTOR = LLMAdapterDescriptor(
    adapter_id="builtin.openai.responses",
    adapter_version="1.0.0",
    display_name={"en": "OpenAI Responses API", "ko": "OpenAI Responses API"},
    config_schema=NO_CONFIG_SCHEMA,
    credential_schema=api_key_schema(required=True),
    features=GenerationFeatures(streaming=True, steering=True),
    options=frozenset({"temperature"}),
    default_base_url=_OPENAI_BASE_URL,
)


class OpenAIResponsesAdapter:
    def __init__(
        self, connection: LLMConnection, transport: LLMTransport, model: str
    ) -> None:
        self._connection = connection
        self._transport = transport
        self.features = GenerationFeatures(
            streaming=True,
            steering=connection.base_url == _OPENAI_BASE_URL and model in _STEER_MODELS,
        )

    async def generate(
        self, request: CanonicalTextRequest, context: ExecutionContext
    ) -> CanonicalTextResult:
        _require_output_limit(request)
        reply = await post_json_object(
            self._transport,
            f"{self._connection.base_url}/responses",
            headers={
                "authorization": f"Bearer {self._connection.credential['api_key']}"
            },
            payload=_payload(request),
            context=context,
            request_id_header="x-request-id",
            refine_error=refine_openai_error,
        )
        return _response_result(reply.body, reply.request_id, status_code=200)

    async def stream(
        self,
        request: CanonicalTextRequest,
        context: ExecutionContext,
        *,
        on_delta: OnDelta,
    ) -> CanonicalTextResult:
        _require_output_limit(request)
        async with open_json_events(
            self._transport,
            f"{self._connection.base_url}/responses",
            headers={
                "authorization": f"Bearer {self._connection.credential['api_key']}"
            },
            payload={**_payload(request), "stream": True},
            context=context,
            request_id_header="x-request-id",
            refine_error=refine_openai_error,
        ) as (events, request_id):
            async for event in events:
                if event is None:
                    break
                kind = event.get("type")
                if kind == "response.output_text.delta":
                    piece = text_of(event.get("delta"))
                    if piece:
                        await on_delta(piece)
                elif kind in (
                    "response.completed",
                    "response.incomplete",
                    "response.failed",
                ):
                    body = mapping(event.get("response"))
                    if not body:
                        raise _stream_error(request_id)
                    return _response_result(body, request_id, status_code=200)
                elif kind == "error":
                    raise LLMAdapterError(
                        refine_openai_error(mapping(event.get("error")) or event)
                        or "provider_error",
                        delivery="sent",
                        provider_request_id=request_id,
                    )
            raise _stream_error(request_id)

    async def stream_with_steering(
        self,
        request: CanonicalTextRequest,
        context: ExecutionContext,
        *,
        on_delta: OnDelta,
        next_steer: NextSteer,
        on_steer: OnSteer,
    ) -> CanonicalTextResult:
        from backend.llm.responses_websocket import stream_responses_websocket

        _require_output_limit(request)
        payload = _payload(request)
        # GPT-6 reasoning does not accept legacy sampling options. A first
        # developer message keeps the instruction prefix for continuation.
        payload.pop("temperature", None)
        payload.pop("instructions", None)
        if request.system:
            payload["input"] = [
                {"role": "developer", "content": request.system},
                *sequence(payload["input"]),
            ]
        return await stream_responses_websocket(
            payload,
            self._connection.credential["api_key"],
            context,
            response_result=_response_result,
            on_delta=on_delta,
            next_steer=next_steer,
            on_steer=on_steer,
        )


def _require_output_limit(request: CanonicalTextRequest) -> None:
    if request.max_output_tokens < _MIN_OUTPUT_TOKENS:
        raise LLMAdapterError("invalid_request", delivery="not_sent")


def _response_result(
    body: Mapping[str, Any],
    request_id: str | None,
    *,
    status_code: int | None = None,
) -> CanonicalTextResult:
    if body.get("status") == "failed":
        # A failed response object can arrive with HTTP 200.
        raise LLMAdapterError(
            refine_openai_error(mapping(body.get("error"))) or "provider_error",
            delivery="sent",
            status_code=status_code,
            provider_request_id=request_id,
        )
    items = [mapping(item) for item in sequence(body.get("output"))]
    if any(not isinstance(item.get("type"), str) for item in items):
        raise LLMAdapterError(
            "invalid_response",
            delivery="sent",
            provider_request_id=request_id,
        )
    parts = [
        mapping(part)
        for item in items
        if item.get("type") == "message"
        for part in sequence(item.get("content"))
    ]
    return CanonicalTextResult(
        text="".join(
            text_of(part.get("text"))
            for part in parts
            if part.get("type") == "output_text"
        ),
        termination=_termination(body, items, parts),
        model=text_of(body.get("model")) or None,
        usage=_usage(mapping(body.get("usage"))),
        provider_request_id=request_id,
    )


def _payload(request: CanonicalTextRequest) -> dict[str, object]:
    payload: dict[str, object] = {
        "model": request.model,
        "input": [
            {"role": message.role, "content": message.text}
            for message in request.messages
        ],
        "max_output_tokens": request.max_output_tokens,
        # Workspace evidence is not retained at the provider for later reuse.
        "store": False,
    }
    if request.system:
        payload["instructions"] = request.system
    if request.temperature is not None:
        payload["temperature"] = request.temperature
    return payload


def _termination(
    body: Mapping[str, Any],
    items: list[Mapping[str, Any]],
    parts: list[Mapping[str, Any]],
) -> Termination:
    return termination(
        _reported_termination(body),
        refused=any(part.get("type") == "refusal" for part in parts),
        tool_request=any(item.get("type") in _CLIENT_TOOL_CALLS for item in items),
    )


def _reported_termination(body: Mapping[str, Any]) -> Termination:
    status = body.get("status")
    if status == "completed":
        return "completed"
    if status == "incomplete":
        reason = text_of(mapping(body.get("incomplete_details")).get("reason"))
        return _INCOMPLETE_TERMINATIONS.get(reason, "unknown")
    return "unknown"


def _usage(usage: Mapping[str, Any]) -> TokenUsage:
    # Cache reads and writes and reasoning output are inside the totals.
    return TokenUsage(
        input_tokens=count(usage.get("input_tokens")),
        output_tokens=count(usage.get("output_tokens")),
        cache_read_input_tokens=count(
            mapping(usage.get("input_tokens_details")).get("cached_tokens")
        ),
        cache_write_input_tokens=count(
            mapping(usage.get("input_tokens_details")).get("cache_write_tokens")
        ),
        reasoning_output_tokens=count(
            mapping(usage.get("output_tokens_details")).get("reasoning_tokens")
        ),
    )


FACTORY = BuiltinAdapterFactory(descriptor=DESCRIPTOR, build=OpenAIResponsesAdapter)

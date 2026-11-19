"""OpenAI Responses API (``POST /v1/responses``)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from backend.llm.adapters._shared import (
    NO_CONFIG_SCHEMA,
    BuiltinAdapterFactory,
    api_key_schema,
    count,
    mapping,
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
    LLMAdapterDescriptor,
    LLMAdapterError,
    LLMConnection,
    LLMTransport,
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

DESCRIPTOR = LLMAdapterDescriptor(
    adapter_id="builtin.openai.responses",
    adapter_version="1.0.0",
    display_name={"en": "OpenAI Responses API", "ko": "OpenAI Responses API"},
    config_schema=NO_CONFIG_SCHEMA,
    credential_schema=api_key_schema(required=True),
    options=frozenset({"temperature"}),
    default_base_url="https://api.openai.com/v1",
)


class OpenAIResponsesAdapter:
    def __init__(self, connection: LLMConnection, transport: LLMTransport) -> None:
        self._connection = connection
        self._transport = transport

    async def generate(
        self, request: CanonicalTextRequest, context: ExecutionContext
    ) -> CanonicalTextResult:
        if request.max_output_tokens < _MIN_OUTPUT_TOKENS:
            raise LLMAdapterError("invalid_request", delivery="not_sent")
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
        body = reply.body
        if body.get("status") == "failed":
            # A failed response object can arrive with HTTP 200.
            raise LLMAdapterError(
                refine_openai_error(mapping(body.get("error"))) or "provider_error",
                delivery="sent",
                status_code=200,
                provider_request_id=reply.request_id,
            )
        items = [mapping(item) for item in sequence(body.get("output"))]
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
            provider_request_id=reply.request_id,
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

"""Chat Completions wire format (``POST /chat/completions``).

One implementation serves two registrations: OpenAI's own endpoint, and
servers that declare compatibility with the format. They differ only in the
values a :class:`ChatCompletionsDialect` holds.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from functools import partial
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
from backend.llm.streaming import OnDelta
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

_TERMINATIONS: Mapping[str, Termination] = {
    "stop": "completed",
    "length": "truncated",
    "content_filter": "blocked",
    "tool_calls": "tool_request",
    "function_call": "tool_request",
}


@dataclass(frozen=True)
class ChatCompletionsDialect:
    # OpenAI deprecated ``max_tokens``; compatible servers still expect it.
    output_budget_field: str


class ChatCompletionsAdapter:
    def __init__(
        self,
        connection: LLMConnection,
        transport: LLMTransport,
        *,
        dialect: ChatCompletionsDialect,
    ) -> None:
        self._connection = connection
        self._transport = transport
        self._dialect = dialect

    async def generate(
        self, request: CanonicalTextRequest, context: ExecutionContext
    ) -> CanonicalTextResult:
        api_key = self._connection.credential.get("api_key")
        reply = await post_json_object(
            self._transport,
            f"{self._connection.base_url}/chat/completions",
            headers={"authorization": f"Bearer {api_key}"} if api_key else {},
            payload=self._payload(request),
            context=context,
            request_id_header="x-request-id",
            refine_error=refine_openai_error,
        )
        body = reply.body
        choices = sequence(body.get("choices"))
        if not choices:
            raise LLMAdapterError(
                "invalid_response",
                delivery="sent",
                status_code=200,
                provider_request_id=reply.request_id,
            )
        choice = mapping(choices[0])
        message = mapping(choice.get("message"))
        return CanonicalTextResult(
            text=text_of(message.get("content")),
            termination=_termination(choice, message),
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
        api_key = self._connection.credential.get("api_key")
        payload = {**self._payload(request), "stream": True}
        # Include final token counts on OpenAI proper. Compatible servers vary
        # in whether they implement the optional stream_options field.
        official = self._dialect.output_budget_field == "max_completion_tokens"
        if official:
            payload["stream_options"] = {"include_usage": True}
        text: list[str] = []
        reported: Termination = "unknown"
        model = None
        usage = TokenUsage()
        refused = False
        requested_tool = False
        saw_done = False
        async with open_json_events(
            self._transport,
            f"{self._connection.base_url}/chat/completions",
            headers={"authorization": f"Bearer {api_key}"} if api_key else {},
            payload=payload,
            context=context,
            request_id_header="x-request-id",
            refine_error=refine_openai_error,
        ) as (events, request_id):
            async for event in events:
                if event is None:
                    saw_done = True
                    break
                if "error" in event:
                    raise LLMAdapterError(
                        refine_openai_error(mapping(event.get("error")))
                        or "provider_error",
                        delivery="sent",
                        provider_request_id=request_id,
                    )
                model = text_of(event.get("model")) or model
                if isinstance(event.get("usage"), Mapping):
                    usage = _usage(mapping(event["usage"]))
                choice = mapping(next(iter(sequence(event.get("choices"))), None))
                delta = mapping(choice.get("delta"))
                refused = refused or bool(text_of(delta.get("refusal")))
                requested_tool = requested_tool or bool(
                    delta.get("tool_calls") or delta.get("function_call")
                )
                piece = text_of(delta.get("content"))
                if piece:
                    text.append(piece)
                    await on_delta(piece)
                reported = _TERMINATIONS.get(
                    text_of(choice.get("finish_reason")), reported
                )
            if reported == "unknown" or (official and not saw_done):
                raise _stream_error(request_id)
            return CanonicalTextResult(
                text="".join(text),
                termination=termination(
                    reported, refused=refused, tool_request=requested_tool
                ),
                model=model,
                usage=usage,
                provider_request_id=request_id,
            )

    def _payload(self, request: CanonicalTextRequest) -> dict[str, object]:
        messages = (
            [{"role": "system", "content": request.system}] if request.system else []
        )
        messages.extend(
            {"role": message.role, "content": message.text}
            for message in request.messages
        )
        payload: dict[str, object] = {
            "model": request.model,
            "messages": messages,
            self._dialect.output_budget_field: request.max_output_tokens,
        }
        if request.temperature is not None:
            payload["temperature"] = request.temperature
        return payload


def _termination(choice: Mapping[str, Any], message: Mapping[str, Any]) -> Termination:
    return termination(
        _TERMINATIONS.get(text_of(choice.get("finish_reason")), "unknown"),
        refused=bool(text_of(message.get("refusal"))),
        tool_request=bool(sequence(message.get("tool_calls"))),
    )


def _usage(usage: Mapping[str, Any]) -> TokenUsage:
    # Cache reads and writes and reasoning tokens are inside the totals.
    return TokenUsage(
        input_tokens=count(usage.get("prompt_tokens")),
        output_tokens=count(usage.get("completion_tokens")),
        cache_read_input_tokens=count(
            mapping(usage.get("prompt_tokens_details")).get("cached_tokens")
        ),
        cache_write_input_tokens=count(
            mapping(usage.get("prompt_tokens_details")).get("cache_write_tokens")
        ),
        reasoning_output_tokens=count(
            mapping(usage.get("completion_tokens_details")).get("reasoning_tokens")
        ),
    )


OPENAI_FACTORY = BuiltinAdapterFactory(
    descriptor=LLMAdapterDescriptor(
        adapter_id="builtin.openai.chat_completions",
        adapter_version="1.0.0",
        display_name={
            "en": "OpenAI Chat Completions API",
            "ko": "OpenAI Chat Completions API",
        },
        config_schema=NO_CONFIG_SCHEMA,
        credential_schema=api_key_schema(required=True),
        options=frozenset({"temperature"}),
        default_base_url="https://api.openai.com/v1",
    ),
    build=partial(
        ChatCompletionsAdapter,
        dialect=ChatCompletionsDialect(output_budget_field="max_completion_tokens"),
    ),
)

COMPATIBLE_FACTORY = BuiltinAdapterFactory(
    descriptor=LLMAdapterDescriptor(
        adapter_id="builtin.openai_compatible.chat_completions",
        adapter_version="1.0.0",
        display_name={
            "en": "OpenAI-compatible Chat Completions",
            "ko": "OpenAI 호환 Chat Completions",
        },
        config_schema=NO_CONFIG_SCHEMA,
        # Self-hosted compatible servers often run without a key.
        credential_schema=api_key_schema(required=False),
        options=frozenset({"temperature"}),
    ),
    build=partial(
        ChatCompletionsAdapter,
        dialect=ChatCompletionsDialect(output_budget_field="max_tokens"),
    ),
)

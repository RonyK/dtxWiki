"""Gemini Developer API ``models/{model}:generateContent`` with an API key."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from backend.llm.adapters._shared import (
    NO_CONFIG_SCHEMA,
    BuiltinAdapterFactory,
    _error_code,
    _stream_error,
    api_key_schema,
    count_or_zero,
    mapping,
    open_json_events,
    post_json_object,
    sequence,
    termination,
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

# The model is a URL path segment; anything else could address another method.
_MODEL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")

# Gemini also uses RESOURCE_EXHAUSTED for temporary limits. Monthly caps are
# identified by the first sentence of the provider's error message.
_MONTHLY_SPENDING_CAP_MESSAGES = (
    "Your billing account has exceeded its monthly spending cap.",
    "Your project has exceeded its monthly spending cap.",
)

_FINISH_TERMINATIONS: Mapping[str, Termination] = {
    "STOP": "completed",
    "MAX_TOKENS": "truncated",
    "CONTINUATION": "truncated",
    "SAFETY": "blocked",
    "RECITATION": "blocked",
    "LANGUAGE": "blocked",
    "BLOCKLIST": "blocked",
    "PROHIBITED_CONTENT": "blocked",
    "SPII": "blocked",
    "IMAGE_SAFETY": "blocked",
    "IMAGE_PROHIBITED_CONTENT": "blocked",
    "IMAGE_RECITATION": "blocked",
    "ESCALATION": "blocked",
    "MALFORMED_FUNCTION_CALL": "tool_request",
    "UNEXPECTED_TOOL_CALL": "tool_request",
    "TOO_MANY_TOOL_CALLS": "tool_request",
}

DESCRIPTOR = LLMAdapterDescriptor(
    adapter_id="builtin.google.gemini_generate_content",
    adapter_version="1.0.0",
    display_name={"en": "Gemini API", "ko": "Gemini API"},
    config_schema=NO_CONFIG_SCHEMA,
    credential_schema=api_key_schema(required=True),
    features=GenerationFeatures(streaming=True, steering=False),
    options=frozenset({"temperature"}),
    default_base_url="https://generativelanguage.googleapis.com/v1beta",
)


class GeminiGenerateContentAdapter:
    features = DESCRIPTOR.features

    def __init__(
        self, connection: LLMConnection, transport: LLMTransport, _model: str
    ) -> None:
        self._connection = connection
        self._transport = transport

    async def generate(
        self, request: CanonicalTextRequest, context: ExecutionContext
    ) -> CanonicalTextResult:
        model = _model_segment(request.model)
        reply = await post_json_object(
            self._transport,
            f"{self._connection.base_url}/models/{model}:generateContent",
            # A header keeps the key out of URLs, proxies and access logs.
            headers={"x-goog-api-key": self._connection.credential["api_key"]},
            payload=_payload(request),
            context=context,
            refine_error=_refine_error,
        )
        body = reply.body
        candidate = mapping(next(iter(sequence(body.get("candidates"))), None))
        parts = [
            mapping(part)
            for part in sequence(mapping(candidate.get("content")).get("parts"))
        ]
        return CanonicalTextResult(
            # Thought summaries are not answer text.
            text="".join(
                text_of(part.get("text")) for part in parts if not part.get("thought")
            ),
            termination=_termination(body, candidate, parts),
            model=text_of(body.get("modelVersion")) or None,
            usage=_usage(body.get("usageMetadata")),
            provider_request_id=text_of(body.get("responseId")) or None,
        )

    async def stream(
        self,
        request: CanonicalTextRequest,
        context: ExecutionContext,
        *,
        on_delta: OnDelta,
    ) -> CanonicalTextResult:
        model = _model_segment(request.model)
        text: list[str] = []
        reported: Termination = "unknown"
        model_version = None
        response_id = None
        usage = TokenUsage()
        async with open_json_events(
            self._transport,
            f"{self._connection.base_url}/models/{model}:streamGenerateContent?alt=sse",
            headers={"x-goog-api-key": self._connection.credential["api_key"]},
            payload=_payload(request),
            context=context,
            refine_error=_refine_error,
        ) as (events, _request_id):
            async for event in events:
                if event is None:
                    break
                response_id = text_of(event.get("responseId")) or response_id
                if "error" in event:
                    error = mapping(event.get("error"))
                    code = error.get("code")
                    raise LLMAdapterError(
                        _error_code(
                            code if type(code) is int else 0, error, _refine_error
                        ),
                        delivery="sent",
                        provider_request_id=response_id,
                    )
                model_version = text_of(event.get("modelVersion")) or model_version
                if "usageMetadata" in event:
                    usage = _usage(event["usageMetadata"])
                candidate = mapping(next(iter(sequence(event.get("candidates"))), None))
                parts = list(
                    map(
                        mapping,
                        sequence(mapping(candidate.get("content")).get("parts")),
                    )
                )
                ending = _termination(event, candidate, parts)
                if ending != "unknown" and reported != "blocked":
                    reported = termination(
                        ending, tool_request=reported == "tool_request"
                    )
                piece = "".join(
                    text_of(part.get("text"))
                    for part in parts
                    if not part.get("thought")
                )
                if piece and reported != "blocked":
                    text.append(piece)
                    await on_delta(piece)
            if reported == "unknown":
                raise _stream_error(response_id)
            return CanonicalTextResult(
                text="".join(text),
                termination=reported,
                model=model_version,
                usage=usage,
                provider_request_id=response_id,
            )


def _model_segment(model: str) -> str:
    # Accept the resource name the Models API lists ("models/<id>") too.
    model = model.removeprefix("models/")
    if not _MODEL_ID.fullmatch(model):
        raise LLMAdapterError("invalid_request", delivery="not_sent")
    return model


def _payload(request: CanonicalTextRequest) -> dict[str, object]:
    generation_config: dict[str, object] = {
        "maxOutputTokens": request.max_output_tokens
    }
    if request.temperature is not None:
        generation_config["temperature"] = request.temperature
    payload: dict[str, object] = {
        "contents": [
            {
                "role": "model" if message.role == "assistant" else "user",
                "parts": [{"text": message.text}],
            }
            for message in request.messages
        ],
        "generationConfig": generation_config,
    }
    if request.system:
        payload["systemInstruction"] = {"parts": [{"text": request.system}]}
    return payload


def _termination(
    body: Mapping[str, Any],
    candidate: Mapping[str, Any],
    parts: list[Mapping[str, Any]],
) -> Termination:
    # A blocked prompt outranks any candidate that came with it.
    block_reason = mapping(body.get("promptFeedback")).get("blockReason")
    if block_reason and block_reason != "BLOCK_REASON_UNSPECIFIED":
        return "blocked"
    if not candidate:
        return "unknown"
    return termination(
        _FINISH_TERMINATIONS.get(text_of(candidate.get("finishReason")), "unknown"),
        tool_request=any("functionCall" in part for part in parts),
    )


def _usage(metadata: object) -> TokenUsage:
    if not isinstance(metadata, Mapping):
        return TokenUsage()
    # The JSON encoding omits zero counts, so an omitted field inside a present
    # usageMetadata is zero. Thought tokens are reported beside the candidate
    # tokens; the prompt count already includes cached content.
    thoughts = count_or_zero(metadata, "thoughtsTokenCount")
    return TokenUsage(
        input_tokens=count_or_zero(metadata, "promptTokenCount"),
        output_tokens=total(count_or_zero(metadata, "candidatesTokenCount"), thoughts),
        cache_read_input_tokens=count_or_zero(metadata, "cachedContentTokenCount"),
        reasoning_output_tokens=thoughts,
    )


def _refine_error(error: Mapping[str, Any]) -> LLMErrorCode | None:
    # An invalid key is reported as 400 INVALID_ARGUMENT with this reason.
    if any(
        mapping(detail).get("reason") == "API_KEY_INVALID"
        for detail in sequence(error.get("details"))
    ):
        return "authentication_failed"
    if (
        error.get("code") == 429
        and error.get("status") == "RESOURCE_EXHAUSTED"
        and text_of(error.get("message")).startswith(_MONTHLY_SPENDING_CAP_MESSAGES)
    ):
        return "quota_exhausted"
    return None


FACTORY = BuiltinAdapterFactory(
    descriptor=DESCRIPTOR, build=GeminiGenerateContentAdapter
)

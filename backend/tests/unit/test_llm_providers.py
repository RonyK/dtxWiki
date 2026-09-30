import asyncio
import json
from uuid import uuid4

import httpx
import pytest

from backend.app.settings import Settings
from backend.llm import (
    AnthropicLLM,
    LLMMessage,
    LLMProviderError,
    LLMRequest,
    LLMToolCall,
    OpenAILLM,
)
from backend.llm.base import EmbeddingProvider, LLMProvider, LLMResponse
from backend.llm.registry import build_llm_provider
from backend.schemas.read import RankedResult, SearchPayload
from backend.services.ask_service import AskService


def test_provider_abcs_are_abstract():
    for provider_cls in [LLMProvider, EmbeddingProvider]:
        with pytest.raises(TypeError):
            provider_cls()


def test_registry_returns_none_by_default_and_builds_configured_providers():
    assert build_llm_provider(Settings()) is None

    anthropic = build_llm_provider(
        Settings(llm_provider="anthropic", anthropic_api_key="anthropic-key")
    )
    openai = build_llm_provider(Settings(llm_provider="openai", openai_api_key="key"))

    assert isinstance(anthropic, AnthropicLLM)
    assert isinstance(openai, OpenAILLM)


def test_registry_rejects_missing_keys_and_unknown_provider():
    with pytest.raises(ValueError, match="Anthropic API key"):
        build_llm_provider(Settings(llm_provider="anthropic"))

    with pytest.raises(ValueError, match="OpenAI API key"):
        build_llm_provider(Settings(llm_provider="openai"))

    with pytest.raises(RuntimeError, match="DTXWIKI_LLM_PROVIDER"):
        build_llm_provider(Settings(llm_provider="local"))


def test_openai_provider_parses_text_and_tool_calls():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer test-key"
        payload = json.loads(request.content)
        assert payload["model"] == "gpt-4o-mini"
        assert payload["messages"][0]["role"] == "user"
        return httpx.Response(
            200,
            json={
                "model": "gpt-4o-mini",
                "choices": [
                    {
                        "finish_reason": "tool_calls",
                        "message": {
                            "content": "answer",
                            "tool_calls": [
                                {
                                    "id": "call-1",
                                    "function": {
                                        "name": "search",
                                        "arguments": '{"query":"needle"}',
                                    },
                                }
                            ],
                        },
                    }
                ],
            },
        )

    provider = OpenAILLM(api_key="test-key", transport=httpx.MockTransport(handler))
    response = provider.chat(
        LLMRequest(messages=[LLMMessage(role="user", content="Needle")])
    )

    assert response.provider == "openai"
    assert response.content == "answer"
    assert response.tool_calls[0].name == "search"
    assert response.tool_calls[0].arguments == {"query": "needle"}
    assert response.stop_reason == "tool_calls"
    assert response.truncated is False


def test_anthropic_provider_parses_text_and_tool_calls():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["x-api-key"] == "test-key"
        payload = json.loads(request.content)
        assert payload["system"] == "system"
        assert payload["messages"][0]["role"] == "user"
        return httpx.Response(
            200,
            json={
                "model": "claude-sonnet-4-6",
                "stop_reason": "tool_use",
                "content": [
                    {"type": "text", "text": "answer"},
                    {
                        "type": "tool_use",
                        "id": "tool-1",
                        "name": "cite",
                        "input": {"claim_text": "needle"},
                    },
                ],
            },
        )

    provider = AnthropicLLM(
        api_key="test-key",
        transport=httpx.MockTransport(handler),
    )
    response = provider.chat(
        LLMRequest(
            messages=[
                LLMMessage(role="system", content="system"),
                LLMMessage(role="user", content="Needle"),
            ]
        )
    )

    assert response.provider == "anthropic"
    assert response.content == "answer"
    assert response.tool_calls[0].name == "cite"
    assert response.tool_calls[0].arguments == {"claim_text": "needle"}
    assert response.stop_reason == "tool_use"
    assert response.truncated is False


def test_stop_reason_surfaces_truncation_for_both_providers():
    def anthropic_handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "model": "claude-sonnet-4-6",
                "stop_reason": "max_tokens",
                "content": [{"type": "text", "text": "cut off mid"}],
            },
        )

    def openai_handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "model": "gpt-4o-mini",
                "choices": [
                    {
                        "finish_reason": "length",
                        "message": {"content": "cut off mid"},
                    }
                ],
            },
        )

    anthropic = AnthropicLLM(
        api_key="test-key",
        transport=httpx.MockTransport(anthropic_handler),
    )
    openai = OpenAILLM(
        api_key="test-key",
        transport=httpx.MockTransport(openai_handler),
    )
    request = LLMRequest(messages=[LLMMessage(role="user", content="x")])

    anthropic_response = anthropic.chat(request)
    openai_response = openai.chat(request)

    assert anthropic_response.stop_reason == "max_tokens"
    assert anthropic_response.truncated is True
    assert openai_response.stop_reason == "length"
    assert openai_response.truncated is True


def test_stop_reason_defaults_to_none_when_provider_omits_it():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "model": "claude-sonnet-4-6",
                "content": [{"type": "text", "text": "answer"}],
            },
        )

    provider = AnthropicLLM(
        api_key="test-key",
        transport=httpx.MockTransport(handler),
    )
    response = provider.chat(
        LLMRequest(messages=[LLMMessage(role="user", content="x")])
    )

    assert response.stop_reason is None
    assert response.truncated is False


def test_openai_async_provider_uses_same_contract():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "model": "gpt-4o-mini",
                "choices": [{"message": {"content": "async answer"}}],
            },
        )

    provider = OpenAILLM(api_key="test-key", transport=httpx.MockTransport(handler))
    response = asyncio.run(
        provider.async_chat(
            LLMRequest(messages=[LLMMessage(role="user", content="Needle")])
        )
    )

    assert response.content == "async answer"


def test_anthropic_retries_429_then_succeeds():
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(
                429,
                headers={"retry-after": "0"},
                json={
                    "type": "error",
                    "error": {"type": "rate_limit_error", "message": "slow down"},
                },
            )
        return httpx.Response(
            200,
            json={
                "model": "claude-sonnet-4-6",
                "content": [{"type": "text", "text": "recovered"}],
            },
        )

    provider = AnthropicLLM(
        api_key="test-key",
        retry_base_delay=0.0,
        transport=httpx.MockTransport(handler),
    )
    response = provider.chat(
        LLMRequest(messages=[LLMMessage(role="user", content="Needle")])
    )

    assert response.content == "recovered"
    assert len(calls) == 2


def test_openai_retries_529_then_succeeds():
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(
                529,
                json={"error": {"type": "overloaded_error", "message": "overloaded"}},
            )
        return httpx.Response(
            200,
            json={
                "model": "gpt-4o-mini",
                "choices": [{"message": {"content": "recovered"}}],
            },
        )

    provider = OpenAILLM(
        api_key="test-key",
        retry_base_delay=0.0,
        transport=httpx.MockTransport(handler),
    )
    response = provider.chat(
        LLMRequest(messages=[LLMMessage(role="user", content="Needle")])
    )

    assert response.content == "recovered"
    assert len(calls) == 2


def test_non_retryable_400_raises_provider_error_with_body_details():
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(
            400,
            json={
                "type": "error",
                "error": {
                    "type": "invalid_request_error",
                    "message": "tool_use ids must match",
                },
            },
        )

    provider = AnthropicLLM(
        api_key="test-key",
        retry_base_delay=0.0,
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(LLMProviderError) as excinfo:
        provider.chat(LLMRequest(messages=[LLMMessage(role="user", content="x")]))

    assert len(calls) == 1
    assert excinfo.value.status_code == 400
    assert excinfo.value.error_type == "invalid_request_error"
    assert excinfo.value.retryable is False
    assert "tool_use ids must match" in str(excinfo.value)


def test_retries_exhausted_raise_provider_error():
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(500, json={"error": {"message": "boom"}})

    provider = OpenAILLM(
        api_key="test-key",
        max_retries=2,
        retry_base_delay=0.0,
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(LLMProviderError) as excinfo:
        provider.chat(LLMRequest(messages=[LLMMessage(role="user", content="x")]))

    assert len(calls) == 3
    assert excinfo.value.status_code == 500
    assert excinfo.value.retryable is True


def test_timeout_is_retried_then_succeeds():
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if len(calls) == 1:
            raise httpx.ReadTimeout("read timed out")
        return httpx.Response(
            200,
            json={
                "model": "gpt-4o-mini",
                "choices": [{"message": {"content": "recovered"}}],
            },
        )

    provider = OpenAILLM(
        api_key="test-key",
        retry_base_delay=0.0,
        transport=httpx.MockTransport(handler),
    )
    response = provider.chat(
        LLMRequest(messages=[LLMMessage(role="user", content="x")])
    )

    assert response.content == "recovered"
    assert len(calls) == 2


def test_timeout_exhaustion_raises_provider_error():
    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("connect timed out")

    provider = AnthropicLLM(
        api_key="test-key",
        max_retries=1,
        retry_base_delay=0.0,
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(LLMProviderError) as excinfo:
        provider.chat(LLMRequest(messages=[LLMMessage(role="user", content="x")]))

    assert excinfo.value.provider == "anthropic"
    assert excinfo.value.retryable is True
    assert "connect timed out" in str(excinfo.value)


def test_async_chat_retries_transient_errors():
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(503, json={"error": {"message": "unavailable"}})
        return httpx.Response(
            200,
            json={
                "model": "claude-sonnet-4-6",
                "content": [{"type": "text", "text": "recovered"}],
            },
        )

    provider = AnthropicLLM(
        api_key="test-key",
        retry_base_delay=0.0,
        transport=httpx.MockTransport(handler),
    )
    response = asyncio.run(
        provider.async_chat(LLMRequest(messages=[LLMMessage(role="user", content="x")]))
    )

    assert response.content == "recovered"
    assert len(calls) == 2


def test_retry_delay_uses_backoff_jitter_and_honors_retry_after():
    provider = OpenAILLM(
        api_key="test-key",
        retry_base_delay=1.0,
        retry_max_delay=8.0,
    )

    for _ in range(20):
        assert 2.0 <= provider._retry_delay(2, None) <= 4.0

    assert provider._retry_delay(0, "3") >= 3.0
    assert provider._retry_delay(0, "120") <= 8.0
    assert provider._retry_delay(0, "not-a-number") <= 1.0


def test_timeout_is_configurable_per_request_with_sane_default():
    provider = AnthropicLLM(api_key="test-key")

    default = provider._request_timeout(
        LLMRequest(messages=[LLMMessage(role="user", content="x")])
    )
    overridden = provider._request_timeout(
        LLMRequest(messages=[LLMMessage(role="user", content="x")], timeout=300.0)
    )

    assert default.read == 120.0
    assert default.connect == 10.0
    assert overridden.read == 300.0
    assert overridden.connect == 10.0


def test_anthropic_serializes_tool_round_trip():
    provider = AnthropicLLM(api_key="test-key")
    request = LLMRequest(
        messages=[
            LLMMessage(role="user", content="Find the needle"),
            LLMMessage(
                role="assistant",
                content="Searching now.",
                tool_calls=[
                    LLMToolCall(
                        call_id="tool-1",
                        name="search",
                        arguments={"query": "needle"},
                    )
                ],
            ),
            LLMMessage(role="tool", content="found it", tool_call_id="tool-1"),
        ]
    )

    payload = provider._payload(request)

    assert payload["messages"][1] == {
        "role": "assistant",
        "content": [
            {"type": "text", "text": "Searching now."},
            {
                "type": "tool_use",
                "id": "tool-1",
                "name": "search",
                "input": {"query": "needle"},
            },
        ],
    }
    assert payload["messages"][2] == {
        "role": "user",
        "content": [
            {
                "type": "tool_result",
                "tool_use_id": "tool-1",
                "content": "found it",
            }
        ],
    }


def test_openai_serializes_tool_round_trip():
    provider = OpenAILLM(api_key="test-key")
    request = LLMRequest(
        messages=[
            LLMMessage(role="user", content="Find the needle"),
            LLMMessage(
                role="assistant",
                content="",
                tool_calls=[
                    LLMToolCall(
                        call_id="call-1",
                        name="search",
                        arguments={"query": "needle"},
                    )
                ],
            ),
            LLMMessage(role="tool", content="found it", tool_call_id="call-1"),
        ]
    )

    payload = provider._payload(request)

    assert payload["messages"][1] == {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": "call-1",
                "type": "function",
                "function": {
                    "name": "search",
                    "arguments": '{"query": "needle"}',
                },
            }
        ],
    }
    assert payload["messages"][2] == {
        "role": "tool",
        "content": "found it",
        "tool_call_id": "call-1",
    }


def test_tool_round_trip_reaches_the_wire_for_both_providers():
    def anthropic_handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assistant = payload["messages"][1]
        assert assistant["content"][0]["type"] == "tool_use"
        assert assistant["content"][0]["id"] == "tool-1"
        assert payload["messages"][2]["content"][0]["tool_use_id"] == "tool-1"
        return httpx.Response(
            200,
            json={
                "model": "claude-sonnet-4-6",
                "content": [{"type": "text", "text": "done"}],
            },
        )

    def openai_handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assistant = payload["messages"][1]
        assert assistant["tool_calls"][0]["id"] == "tool-1"
        assert payload["messages"][2]["tool_call_id"] == "tool-1"
        return httpx.Response(
            200,
            json={
                "model": "gpt-4o-mini",
                "choices": [{"message": {"content": "done"}}],
            },
        )

    messages = [
        LLMMessage(role="user", content="Find the needle"),
        LLMMessage(
            role="assistant",
            content="",
            tool_calls=[LLMToolCall(call_id="tool-1", name="search")],
        ),
        LLMMessage(role="tool", content="found it", tool_call_id="tool-1"),
    ]

    anthropic = AnthropicLLM(
        api_key="test-key",
        transport=httpx.MockTransport(anthropic_handler),
    )
    openai = OpenAILLM(
        api_key="test-key",
        transport=httpx.MockTransport(openai_handler),
    )

    assert anthropic.chat(LLMRequest(messages=messages)).content == "done"
    assert openai.chat(LLMRequest(messages=messages)).content == "done"


def test_ask_service_composes_from_evidence_when_provider_is_set():
    result_id = uuid4()
    search_service = FakeSearchService(
        SearchPayload(
            ranked_results=[
                RankedResult(
                    id=result_id,
                    type="context_page",
                    title="Needle Context",
                    snippet="Needle evidence body.",
                    score=1.0,
                    citation_available=True,
                )
            ],
            supported_retrieval_modes=["keyword"],
            permission_note=None,
            next_cursor=None,
        )
    )
    service = AskService(
        search_service=search_service,
        provider=FixedProvider("Composed answer [1]."),
    )

    (
        answer,
        context_ids,
        artifact_ids,
        trace,
        lanes,
        fallback,
        nearest,
        warnings,
    ) = service._answer(
        session=_NoPolicySession(), workspace_id=uuid4(), question="Needle"
    )

    assert answer == "Composed answer [1]."
    assert context_ids == [result_id]
    assert artifact_ids == []
    assert trace == [
        {
            "id": str(result_id),
            "type": "context_page",
            "title": "Needle Context",
            "score": "1.00",
            "match_reason": "",
            "concept_path": "",
        }
    ]
    assert lanes == []
    assert fallback is False
    assert nearest == []
    assert warnings == []


@pytest.mark.parametrize("provider", ["error", "truncated", "unexpected"])
def test_ask_service_falls_back_to_deterministic_pack_on_provider_failure(provider):
    result_id = uuid4()
    service = AskService(
        search_service=FakeSearchService(
            SearchPayload(
                ranked_results=[
                    RankedResult(
                        id=result_id,
                        type="context_page",
                        title="Needle Context",
                        snippet="Needle evidence body.",
                        score=1.0,
                        citation_available=True,
                    )
                ],
                supported_retrieval_modes=["keyword"],
                permission_note=None,
                next_cursor=None,
            )
        ),
        provider=(
            ErrorProvider()
            if provider == "error"
            else (
                UnexpectedErrorProvider()
                if provider == "unexpected"
                else FixedProvider("Incomplete", stop_reason="length")
            )
        ),
    )

    answer, *_ = service._answer(
        session=_NoPolicySession(),
        workspace_id=uuid4(),
        question="Needle",
    )

    assert answer == (
        "Context-first retrieval summary:\n1. Needle Context: Needle evidence body."
    )


class _NoPolicySession:
    """The only thing `_answer` asks a session for: this workspace's retrieval
    policy. These tests are about the composer, so there is none and Ask falls
    back to the ladder it has always used."""

    def scalar(self, *_args, **_kwargs):
        return None


class FakeSearchService:
    def __init__(self, payload: SearchPayload) -> None:
        self.payload = payload

    def search(self, **_kwargs) -> SearchPayload:
        return self.payload


class FixedProvider(LLMProvider):
    def __init__(self, content: str, *, stop_reason: str | None = None) -> None:
        self.content = content
        self.stop_reason = stop_reason

    def chat(self, request: LLMRequest):
        assert "[Evidence pack]" in request.messages[-1].content
        assert (
            "1. Needle Context: Needle evidence body." in request.messages[-1].content
        )
        return LLMResponse(
            provider="test",
            model="test",
            content=self.content,
            stop_reason=self.stop_reason,
        )

    async def async_chat(self, request: LLMRequest):
        return self.chat(request)


class ErrorProvider(LLMProvider):
    def chat(self, request: LLMRequest):
        raise LLMProviderError("failed", provider="test")

    async def async_chat(self, request: LLMRequest):
        return self.chat(request)


class UnexpectedErrorProvider(LLMProvider):
    def chat(self, request: LLMRequest):
        raise RuntimeError("unexpected provider failure")

    async def async_chat(self, request: LLMRequest):
        return self.chat(request)

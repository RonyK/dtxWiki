import asyncio
import json
from dataclasses import replace
from uuid import uuid4

import httpx
import pytest

from backend.app.settings import Settings
from backend.llm.adapter_registry import LLMAdapterRegistry, LLMAdapterRegistryError
from backend.llm.adapters import (
    anthropic_messages,
    build_builtin_llm_adapters,
    chat_completions,
    gemini_generate_content,
    openai_responses,
)
from backend.llm.adapters._shared import BuiltinAdapterFactory
from backend.llm.base import EmbeddingProvider
from backend.llm.generation import BlockingTextGenerator
from backend.llm.legacy import RetryingTextGeneration, build_llm_provider
from backend.llm.transport import HttpxLLMTransport
from backend.schemas.read import RankedResult, SearchPayload
from backend.services.ask_service import AskService
from backend.spi.llm import (
    CanonicalTextRequest,
    CanonicalTextResult,
    ExecutionContext,
    LLMAdapterError,
    LLMModelCapabilities,
    TextMessage,
    TokenUsage,
    TransportResponse,
)
from backend.testing.spi_conformance import WireCase, assert_llm_adapter_contract

KEY = "secret-test-key"
REQUEST = CanonicalTextRequest(
    model="model-x",
    messages=(TextMessage(role="user", text="Needle?"),),
    max_output_tokens=64,
    system="Be brief.",
)
WARM_REQUEST = CanonicalTextRequest(
    model="model-x",
    messages=(TextMessage(role="user", text="Needle?"),),
    max_output_tokens=64,
    temperature=0.2,
)


def _reply(body, *, status=200, headers=None) -> TransportResponse:
    return TransportResponse(
        status_code=status,
        headers=headers or {},
        body=json.dumps(body).encode(),
    )


def _builtin_registry() -> LLMAdapterRegistry:
    return LLMAdapterRegistry(build_builtin_llm_adapters())


class RecordingTransport:
    def __init__(self, reply: TransportResponse) -> None:
        self.reply = reply
        self.calls: list[dict] = []

    async def post_json(self, url, *, headers, payload, timeout_seconds):
        self.calls.append({"url": url, "headers": dict(headers), "payload": payload})
        return self.reply


def test_provider_abcs_are_abstract():
    with pytest.raises(TypeError):
        EmbeddingProvider()


def test_registry_returns_none_by_default_and_builds_configured_providers():
    registry = _builtin_registry()
    assert build_llm_provider(Settings(_env_file=None), registry) is None

    # Each accepted value keeps the model, temperature and token budget the
    # server-wide answer model always used, on its built-in adapter.
    for selection, key_field, url, model, budget_field, reply in (
        (
            "anthropic",
            "anthropic_api_key",
            "https://api.anthropic.com/v1/messages",
            "claude-sonnet-4-6",
            "max_tokens",
            {"content": [{"type": "text", "text": "ok"}], "stop_reason": "end_turn"},
        ),
        (
            "openai",
            "openai_api_key",
            "https://api.openai.com/v1/chat/completions",
            "gpt-4o-mini",
            "max_completion_tokens",
            {"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]},
        ),
    ):
        transport = RecordingTransport(_reply(reply))
        generator = build_llm_provider(
            Settings(_env_file=None, llm_provider=selection, **{key_field: KEY}),
            registry,
            transport=transport,
        )
        result = generator.generate(
            system="sys",
            messages=(TextMessage(role="user", text="q"),),
            max_output_tokens=1024,
        )
        (call,) = transport.calls
        assert (result.text, result.termination) == ("ok", "completed")
        assert call["url"] == url
        assert call["payload"]["model"] == model
        assert call["payload"]["temperature"] == 0.0
        assert call["payload"][budget_field] == 1024

    # An option the model cannot honor is refused before anything is sent.
    transport = RecordingTransport(_reply({}))
    unsupported = BlockingTextGenerator(
        port=registry.create(
            "builtin.anthropic.messages",
            base_url=None,
            config={},
            credential={"api_key": KEY},
            model="model-x",
            capabilities=LLMModelCapabilities(),
            transport=transport,
        ),
        model="model-x",
        context=ExecutionContext(timeout_seconds=5),
        temperature=0.0,
    )
    with pytest.raises(LLMAdapterError) as error:
        unsupported.generate(
            system=None,
            messages=(TextMessage(role="user", text="q"),),
            max_output_tokens=8,
        )
    assert (error.value.code, error.value.delivery) == (
        "unsupported_option",
        "not_sent",
    )
    assert transport.calls == []


def test_registry_rejects_missing_keys_and_unknown_provider():
    registry = _builtin_registry()
    with pytest.raises(ValueError, match="DTXWIKI_ANTHROPIC_API_KEY"):
        build_llm_provider(Settings(_env_file=None, llm_provider="anthropic"), registry)

    with pytest.raises(ValueError, match="DTXWIKI_OPENAI_API_KEY"):
        build_llm_provider(Settings(_env_file=None, llm_provider="openai"), registry)

    with pytest.raises(RuntimeError, match="DTXWIKI_LLM_PROVIDER"):
        build_llm_provider(Settings(_env_file=None, llm_provider="local"), registry)


class ScriptedPort:
    def __init__(self, outcomes) -> None:
        self.outcomes = list(outcomes)
        self.calls = 0

    async def generate(self, request, context):
        outcome = self.outcomes[self.calls]
        self.calls += 1
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def test_legacy_retry_policy_keeps_its_statuses_and_backoff():
    """The server-wide model keeps its two retries; adapters never retry."""

    def failure(code, status=None, retry_after=None):
        return LLMAdapterError(
            code,
            delivery="sent" if status else "unknown",
            status_code=status,
            retry_after_seconds=retry_after,
        )

    async def run(port):
        try:
            return await port.generate(REQUEST, ExecutionContext(5))
        except LLMAdapterError as exc:
            return exc

    ok = CanonicalTextResult(text="ok", termination="completed")
    for outcomes, expected_calls, expected_sleeps, expected in (
        # Retries follow the old statuses, not the normalized code: a 429 is
        # retried even as quota_exhausted, honoring Retry-After (capped at
        # 8 s); a 503 backs off; then success.
        (
            [
                failure("quota_exhausted", 429, 30.0),
                failure("provider_unavailable", 503),
                ok,
            ],
            3,
            [8.0, 1.0],
            ok,
        ),
        # A 401 is not transient, and neither was a 501.
        ([failure("authentication_failed", 401)], 1, [], "authentication_failed"),
        ([failure("provider_unavailable", 501)], 1, [], "provider_unavailable"),
        # A status-less transport timeout is retried until the budget runs out.
        ([failure("timeout")] * 3, 3, [0.5, 1.0], "timeout"),
    ):
        port = ScriptedPort(outcomes)
        sleeps: list[float] = []

        async def sleep(seconds, sleeps=sleeps):
            sleeps.append(seconds)

        outcome = asyncio.run(
            run(RetryingTextGeneration(port, sleep=sleep, jitter=lambda: 1.0))
        )
        assert getattr(outcome, "code", outcome) == expected
        assert (port.calls, sleeps) == (expected_calls, expected_sleeps)


def _anthropic_cases():
    def wire(call):
        assert call.url == "https://api.anthropic.com/v1/messages"
        assert call.headers == {"x-api-key": KEY, "anthropic-version": "2023-06-01"}
        assert call.payload == {
            "model": "model-x",
            "max_tokens": 64,
            "messages": [{"role": "user", "content": "Needle?"}],
            "system": "Be brief.",
        }

    return [
        WireCase(
            "completed with cache and thinking usage",
            REQUEST,
            _reply(
                {
                    "model": "claude-x",
                    "stop_reason": "end_turn",
                    "content": [
                        {"type": "thinking", "thinking": "hidden"},
                        {"type": "text", "text": "An "},
                        {"type": "text", "text": "answer."},
                    ],
                    "usage": {
                        "input_tokens": 10,
                        "cache_creation_input_tokens": 3,
                        "cache_read_input_tokens": 5,
                        "output_tokens": 7,
                        "output_tokens_details": {"thinking_tokens": 2},
                    },
                },
                headers={"Request-Id": "req_1"},
            ),
            expected=CanonicalTextResult(
                text="An answer.",
                termination="completed",
                model="claude-x",
                usage=TokenUsage(18, 7, 5, 3, 2),
                provider_request_id="req_1",
            ),
            check_wire=wire,
        ),
        WireCase(
            "truncated without usage",
            REQUEST,
            _reply(
                {
                    "stop_reason": "max_tokens",
                    "content": [{"type": "text", "text": "Par"}],
                }
            ),
            expected=CanonicalTextResult(text="Par", termination="truncated"),
        ),
        WireCase(
            "refusal",
            REQUEST,
            _reply(
                {"stop_reason": "refusal", "content": [{"type": "text", "text": "No"}]}
            ),
            expected=CanonicalTextResult(text="No", termination="refused"),
        ),
        WireCase(
            "spend limit on 429 is not a rate limit",
            REQUEST,
            _reply(
                {
                    "error": {
                        "type": "rate_limit_error",
                        "details": {"error_code": "contract_spend_limit_reached"},
                    }
                },
                status=429,
            ),
            expected_error=("quota_exhausted", "sent"),
        ),
        WireCase(
            "rate limited",
            REQUEST,
            _reply({"error": {"type": "rate_limit_error"}}, status=429),
            expected_error=("rate_limited", "sent"),
        ),
    ]


def _responses_cases():
    def wire(call):
        assert call.url == "https://api.openai.com/v1/responses"
        assert call.headers == {"authorization": f"Bearer {KEY}"}
        assert call.payload == {
            "model": "model-x",
            "input": [{"role": "user", "content": "Needle?"}],
            "max_output_tokens": 64,
            "store": False,
            "temperature": 0.2,
        }

    return [
        WireCase(
            "completed around a reasoning item",
            WARM_REQUEST,
            _reply(
                {
                    "model": "gpt-x",
                    "status": "completed",
                    "output": [
                        {"type": "reasoning", "summary": []},
                        {
                            "type": "message",
                            "content": [{"type": "output_text", "text": "Answer."}],
                        },
                    ],
                    "usage": {
                        "input_tokens": 12,
                        "input_tokens_details": {
                            "cached_tokens": 4,
                            "cache_write_tokens": 3,
                        },
                        "output_tokens": 9,
                        "output_tokens_details": {"reasoning_tokens": 6},
                    },
                },
                headers={"x-request-id": "req_2"},
            ),
            expected=CanonicalTextResult(
                text="Answer.",
                termination="completed",
                model="gpt-x",
                usage=TokenUsage(12, 9, 4, 3, 6),
                provider_request_id="req_2",
            ),
            check_wire=wire,
        ),
        WireCase(
            "incomplete at the output budget",
            REQUEST,
            _reply(
                {
                    "status": "incomplete",
                    "incomplete_details": {"reason": "max_output_tokens"},
                    "output": [],
                }
            ),
            expected=CanonicalTextResult(text="", termination="truncated"),
        ),
        WireCase(
            "refusal part outranks completion",
            REQUEST,
            _reply(
                {
                    "status": "completed",
                    "output": [
                        {
                            "type": "message",
                            "content": [{"type": "refusal", "refusal": "No."}],
                        }
                    ],
                }
            ),
            expected=CanonicalTextResult(text="", termination="refused"),
        ),
        WireCase(
            "failed response object on HTTP 200",
            REQUEST,
            _reply({"status": "failed", "error": {"code": "invalid_prompt"}}),
            expected_error=("invalid_request", "sent"),
        ),
        WireCase(
            "a budget below the API minimum is never sent",
            CanonicalTextRequest(
                model="model-x",
                messages=(TextMessage(role="user", text="q"),),
                max_output_tokens=8,
            ),
            None,
            expected_error=("invalid_request", "not_sent"),
            expected_sends=0,
        ),
        WireCase(
            "spend limit on 429 is not a rate limit",
            REQUEST,
            _reply({"error": {"code": "project_spend_limit_exceeded"}}, status=429),
            expected_error=("quota_exhausted", "sent"),
        ),
    ]


def _chat_cases():
    def wire(call):
        assert call.url == "https://api.openai.com/v1/chat/completions"
        assert call.headers == {"authorization": f"Bearer {KEY}"}
        assert call.payload == {
            "model": "model-x",
            "messages": [
                {"role": "system", "content": "Be brief."},
                {"role": "user", "content": "Needle?"},
            ],
            "max_completion_tokens": 64,
        }

    def choice(finish, **message):
        return _reply({"choices": [{"finish_reason": finish, "message": message}]})

    return [
        WireCase(
            "completed with usage details",
            REQUEST,
            _reply(
                {
                    "model": "gpt-x",
                    "choices": [
                        {"finish_reason": "stop", "message": {"content": "Answer."}}
                    ],
                    "usage": {
                        "prompt_tokens": 11,
                        "prompt_tokens_details": {"cached_tokens": 2},
                        "completion_tokens": 5,
                        "completion_tokens_details": {"reasoning_tokens": 1},
                    },
                }
            ),
            expected=CanonicalTextResult(
                text="Answer.",
                termination="completed",
                model="gpt-x",
                usage=TokenUsage(11, 5, 2, None, 1),
            ),
            check_wire=wire,
        ),
        WireCase(
            "length",
            REQUEST,
            choice("length", content="Par"),
            expected=CanonicalTextResult(text="Par", termination="truncated"),
        ),
        WireCase(
            "content filter outranks a tool call",
            REQUEST,
            choice("content_filter", content="", tool_calls=[{"id": "t"}]),
            expected=CanonicalTextResult(text="", termination="blocked"),
        ),
        WireCase(
            "no choices",
            REQUEST,
            _reply({"choices": []}),
            expected_error=("invalid_response", "sent"),
        ),
    ]


def _gemini_cases():
    def wire(call):
        assert call.url == (
            "https://generativelanguage.googleapis.com/v1beta/models/"
            "model-x:generateContent"
        )
        assert call.headers == {"x-goog-api-key": KEY}
        assert call.payload == {
            "contents": [{"role": "user", "parts": [{"text": "Needle?"}]}],
            "generationConfig": {"maxOutputTokens": 64},
            "systemInstruction": {"parts": [{"text": "Be brief."}]},
        }

    def candidate(finish, *parts, **body):
        return _reply(
            {
                "candidates": [
                    {"finishReason": finish, "content": {"parts": list(parts)}}
                ],
                **body,
            }
        )

    return [
        WireCase(
            "resource-name model; no thought text; omitted zero counts",
            replace(REQUEST, model="models/model-x"),
            candidate(
                "STOP",
                {"text": "plan", "thought": True},
                {"text": "Answer."},
                modelVersion="gemini-x-001",
                responseId="resp_3",
                usageMetadata={
                    "promptTokenCount": 9,
                    "candidatesTokenCount": 4,
                    "thoughtsTokenCount": 3,
                },
            ),
            expected=CanonicalTextResult(
                text="Answer.",
                termination="completed",
                model="gemini-x-001",
                usage=TokenUsage(9, 7, 0, None, 3),
                provider_request_id="resp_3",
            ),
            check_wire=wire,
        ),
        WireCase(
            "max tokens; a malformed count stays unknown",
            REQUEST,
            candidate(
                "MAX_TOKENS",
                {"text": "Par"},
                usageMetadata={"promptTokenCount": 5, "candidatesTokenCount": "4"},
            ),
            expected=CanonicalTextResult(
                text="Par",
                termination="truncated",
                usage=TokenUsage(5, None, 0, None, 0),
            ),
        ),
        WireCase(
            "blocked prompt outranks a candidate",
            REQUEST,
            candidate("STOP", {"text": "x"}, promptFeedback={"blockReason": "SAFETY"}),
            expected=CanonicalTextResult(text="x", termination="blocked"),
        ),
        WireCase(
            "a model id that would leave the path is never sent",
            CanonicalTextRequest(
                model="../files",
                messages=(TextMessage(role="user", text="q"),),
                max_output_tokens=8,
            ),
            None,
            expected_error=("invalid_request", "not_sent"),
            expected_sends=0,
        ),
        WireCase(
            "invalid API key reported as 400",
            REQUEST,
            _reply(
                {
                    "error": {
                        "status": "INVALID_ARGUMENT",
                        "details": [{"reason": "API_KEY_INVALID"}],
                    }
                },
                status=400,
            ),
            expected_error=("authentication_failed", "sent"),
        ),
    ]


def _compatible_cases():
    def wire(call):
        assert call.url == "https://llm.internal.example/v1/chat/completions"
        assert call.headers == {}
        assert call.payload["max_tokens"] == 64

    return [
        WireCase(
            "keyless compatible server",
            REQUEST,
            _reply(
                {"choices": [{"finish_reason": "stop", "message": {"content": "ok"}}]}
            ),
            expected=CanonicalTextResult(text="ok", termination="completed"),
            check_wire=wire,
        )
    ]


# Budget exception: each built-in registration is an independent wire format
# (the compatible one differs in endpoint, auth and budget field), so a mapping
# bug in one is invisible to the others. Each adapter runs the design's
# conformance list once (completed with usage and wire, truncated, refused or
# blocked, a normalized error) plus only the edges its own format has (missing
# or malformed usage, precedence, spend caps, a 200 failure, local refusals).
# The cases replace the former per-provider parse, truncation, retry and tool
# tests.
@pytest.mark.parametrize(
    ("factory", "cases", "connection"),
    [
        (
            anthropic_messages.FACTORY,
            _anthropic_cases,
            {"credential": {"api_key": KEY}},
        ),
        (openai_responses.FACTORY, _responses_cases, {"credential": {"api_key": KEY}}),
        (
            chat_completions.OPENAI_FACTORY,
            _chat_cases,
            {"credential": {"api_key": KEY}},
        ),
        (
            chat_completions.COMPATIBLE_FACTORY,
            _compatible_cases,
            {"credential": {}, "base_url": "https://llm.internal.example/v1/"},
        ),
        (
            gemini_generate_content.FACTORY,
            _gemini_cases,
            {"credential": {"api_key": KEY}},
        ),
    ],
    ids=["anthropic", "responses", "chat", "compatible-chat", "gemini"],
)
def test_builtin_llm_adapters_meet_the_contract(factory, cases, connection):
    asyncio.run(
        assert_llm_adapter_contract(
            factory, namespace="builtin", cases=cases(), **connection
        )
    )


def test_llm_adapter_registry_rejects_unsafe_registrations():
    descriptor = anthropic_messages.DESCRIPTOR

    def factory(build=anthropic_messages.AnthropicMessagesAdapter, **changes):
        return BuiltinAdapterFactory(
            descriptor=replace(descriptor, **changes), build=build
        )

    class OneArgumentFactory:
        def __init__(self) -> None:
            self.descriptor = descriptor

        def create(self, connection):
            raise AssertionError("never registered")

    for builtin, plugins in (
        ((), {"vendor": [factory()]}),  # a plugin claiming builtin.*
        ((), {"builtin": [factory()]}),  # a plugin named "builtin"
        ([factory(adapter_id="anthropic")], {}),  # not namespaced
        ([factory(), factory()], {}),  # duplicate id
        ([factory(spi_version="2.0")], {}),
        ([factory(adapter_version="latest")], {}),
        ([factory(config_schema={"type": "nonsense"})], {}),
        # References can fetch, miss or loop: forms must be self-contained.
        ([factory(credential_schema={"$ref": "#"})], {}),
        ([factory(options=frozenset({"top_k"}))], {}),
        ([factory(default_base_url="http://api.example/v1")], {}),
        ([factory(default_base_url="https://api.example/v1?")], {}),
        ([OneArgumentFactory()], {}),
        ([object()], {}),
    ):
        with pytest.raises(LLMAdapterRegistryError):
            LLMAdapterRegistry(builtin, plugins)

    def create(registry, adapter_id, **overrides):
        arguments = {
            "base_url": None,
            "config": {},
            "credential": {"api_key": KEY},
            "model": REQUEST.model,
            "capabilities": LLMModelCapabilities(),
            "transport": RecordingTransport(_reply({})),
        }
        return registry.create(adapter_id, **{**arguments, **overrides})

    registry = LLMAdapterRegistry(
        [
            factory(),
            factory(
                adapter_id="builtin.plain", default_base_url=None, options=frozenset()
            ),
        ],
        {"vendor": [factory(adapter_id="vendor.custom_text")]},
    )
    assert [item.adapter_id for item in registry.descriptors()] == [
        "builtin.anthropic.messages",
        "builtin.plain",
        "vendor.custom_text",
    ]
    # A registration without a default endpoint needs one from its connection,
    # and a model cannot use an option its adapter cannot encode.
    with pytest.raises(LLMAdapterRegistryError, match="https base URL"):
        create(registry, "builtin.plain")
    with pytest.raises(LLMAdapterRegistryError, match="options"):
        create(
            registry,
            "builtin.plain",
            base_url="https://api.example/v1",
            capabilities=LLMModelCapabilities(options=frozenset({"temperature"})),
        )
    # A port serves the one model its capabilities describe.
    port = create(registry, "builtin.anthropic.messages", model="other-model")
    with pytest.raises(LLMAdapterError) as other_model:
        asyncio.run(port.generate(REQUEST, ExecutionContext(5)))
    assert (other_model.value.code, other_model.value.delivery) == (
        "invalid_request",
        "not_sent",
    )
    # Schema errors never echo the submitted credential.
    with pytest.raises(LLMAdapterRegistryError) as rejected:
        create(registry, "vendor.custom_text", credential={"api_key": KEY, "x": KEY})
    assert KEY not in str(rejected.value)

    class WrongSignature:
        def __init__(self, connection, transport):
            pass

        async def generate(self, request):
            return None

    with pytest.raises(LLMAdapterRegistryError, match="text generation port"):
        create(
            LLMAdapterRegistry([factory(build=WrongSignature)]), descriptor.adapter_id
        )

    # A provider request ID that carries the credential is dropped.
    class EchoingKey:
        def __init__(self, connection, transport):
            pass

        async def generate(self, request, context):
            return CanonicalTextResult(
                text="", termination="completed", provider_request_id=KEY
            )

    port = create(
        LLMAdapterRegistry([factory(build=EchoingKey)]), descriptor.adapter_id
    )
    result = asyncio.run(port.generate(REQUEST, ExecutionContext(5)))
    assert result.provider_request_id is None

    # Core keeps every call of an execution under its endpoint, without the
    # credential in the URL or a rerouting Host header.
    for url, headers in (
        ("https://api.anthropic.com/v1/%2e%2e/%2E%2E/evil", {}),
        ("https://api.anthropic.com/v1/messages", {"Host": "evil.example"}),
        (f"https://api.anthropic.com/v1/messages?key={KEY}", {}),
    ):

        class Exfiltrating:
            def __init__(self, connection, transport, url=url, headers=headers):
                self.transport, self.url, self.headers = transport, url, headers

            async def generate(self, request, context):
                await self.transport.post_json(
                    self.url, headers=self.headers, payload={}, timeout_seconds=1
                )

        transport = RecordingTransport(_reply({}))
        port = create(
            LLMAdapterRegistry([factory(build=Exfiltrating)]),
            descriptor.adapter_id,
            transport=transport,
        )
        with pytest.raises(LLMAdapterError) as escaped:
            asyncio.run(port.generate(REQUEST, ExecutionContext(5)))
        assert (escaped.value.code, escaped.value.delivery) == (
            "invalid_request",
            "not_sent",
        )
        assert transport.calls == []


# Budget exception (transport): each row is a different delivery proof the
# legacy retry and later billing decisions rely on; none is visible above the
# transport.
def test_httpx_transport_reports_what_reached_the_provider():
    def raising(exc):
        def handler(request):
            raise exc

        return handler

    class InterruptedBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'{"partial":'
            raise httpx.ReadTimeout("body stalled")

    def interrupted_reply(_request):
        return httpx.Response(200, stream=InterruptedBody())

    for handler, expected in (
        (raising(httpx.ConnectError("refused")), ("network_error", "not_sent", None)),
        (raising(httpx.ReadTimeout("slow")), ("timeout", "unknown", None)),
        (
            raising(httpx.RemoteProtocolError("reset")),
            ("network_error", "unknown", None),
        ),
        (interrupted_reply, ("timeout", "sent", 200)),
    ):
        transport = HttpxLLMTransport(transport=httpx.MockTransport(handler))
        with pytest.raises(LLMAdapterError) as error:
            asyncio.run(
                transport.post_json(
                    "https://api.example/v1/x",
                    headers={},
                    payload={},
                    timeout_seconds=1,
                )
            )
        assert (
            error.value.code,
            error.value.delivery,
            error.value.status_code,
        ) == expected

    def answer(request):
        assert json.loads(request.content) == {"q": 1}
        return httpx.Response(200, headers={"X-Request-Id": "r"}, json={"ok": True})

    # A reply already read survives a failed connection teardown: reporting
    # it as an unknown outcome would make the caller send it again.
    class ClosingFails(httpx.MockTransport):
        async def aclose(self):
            raise httpx.CloseError("close failed")

    response = asyncio.run(
        HttpxLLMTransport(transport=ClosingFails(answer)).post_json(
            "https://api.example/v1/x", headers={}, payload={"q": 1}, timeout_seconds=1
        )
    )
    assert (response.status_code, response.headers["x-request-id"]) == (200, "r")


def test_plugin_adapter_can_post_to_its_configured_endpoint():
    endpoint = "https://api.example/generate"

    class ExactEndpointAdapter:
        def __init__(self, connection, transport):
            self.connection = connection
            self.transport = transport

        async def generate(self, request, context):
            await self.transport.post_json(
                self.connection.base_url,
                headers={},
                payload={"prompt": request.messages[0].text},
                timeout_seconds=context.timeout_seconds,
            )
            return CanonicalTextResult(text="answer", termination="completed")

    descriptor = replace(
        chat_completions.COMPATIBLE_FACTORY.descriptor,
        adapter_id="vendor.generation",
        default_base_url=endpoint,
    )
    factory = BuiltinAdapterFactory(descriptor, ExactEndpointAdapter)
    transport = RecordingTransport(_reply({}))
    port = LLMAdapterRegistry(plugins={"vendor": [factory]}).create(
        descriptor.adapter_id,
        base_url=None,
        config={},
        credential={},
        model=REQUEST.model,
        capabilities=LLMModelCapabilities(),
        transport=transport,
    )

    assert asyncio.run(port.generate(REQUEST, ExecutionContext(5))).text == "answer"
    assert [call["url"] for call in transport.calls] == [endpoint]


def test_error_response_keeps_status_and_retry_metadata_on_decoding_failure():
    def unavailable(_request):
        return httpx.Response(
            503,
            headers={
                "content-encoding": "gzip",
                "retry-after": "4",
                "x-request-id": "upstream-request",
            },
            stream=httpx.ByteStream(b"invalid gzip"),
        )

    port = _builtin_registry().create(
        "builtin.openai.chat_completions",
        base_url=None,
        config={},
        credential={"api_key": KEY},
        model=REQUEST.model,
        capabilities=LLMModelCapabilities(),
        transport=HttpxLLMTransport(transport=httpx.MockTransport(unavailable)),
    )

    with pytest.raises(LLMAdapterError) as failure:
        asyncio.run(port.generate(REQUEST, ExecutionContext(5)))

    error = failure.value
    assert (error.code, error.status_code, error.delivery) == (
        "provider_unavailable",
        503,
        "sent",
    )
    assert error.retry_after_seconds == 4
    assert error.provider_request_id == "upstream-request"


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
            "stable_url": "",
        }
    ]
    assert lanes == []
    assert fallback is False
    assert nearest == []
    assert warnings == []


# Each case is one thing the reader can do about the fallback: try again
# (a 503), nothing (a cut-off reply), or ask an administrator (a rejected key,
# or a bug in the composer itself). A refused reply is checked end to end in
# test_ask_llm_degrade, where the turn's warning allowlist applies.
@pytest.mark.parametrize(
    ("provider", "warning"),
    [
        (lambda: ErrorProvider(503, "provider_unavailable"), "llm_composer_retryable"),
        (
            lambda: ErrorProvider(401, "authentication_failed"),
            "llm_provider_unavailable",
        ),
        (
            lambda: FixedProvider("Incomplete", termination="truncated"),
            "llm_answer_incomplete",
        ),
        (lambda: UnexpectedErrorProvider(), "llm_provider_unavailable"),
    ],
    ids=["temporary", "credential", "truncated", "unexpected"],
)
def test_ask_service_falls_back_to_deterministic_pack_on_provider_failure(
    provider, warning
):
    service = AskService(
        search_service=FakeSearchService(
            SearchPayload(
                ranked_results=[
                    RankedResult(
                        id=uuid4(),
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
        provider=provider(),
    )

    answer, *_, warnings = service._answer(
        session=_NoPolicySession(),
        workspace_id=uuid4(),
        question="Needle",
    )

    assert answer == (
        "Context-first retrieval summary:\n1. Needle Context: Needle evidence body."
    )
    assert warnings == [warning]


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


class FixedProvider:
    def __init__(self, text: str, *, termination: str = "completed") -> None:
        self.text = text
        self.termination = termination

    def generate(self, *, system, messages, max_output_tokens):
        assert "[Evidence pack]" in messages[-1].text
        assert "1. Needle Context: Needle evidence body." in messages[-1].text
        return CanonicalTextResult(text=self.text, termination=self.termination)


class ErrorProvider:
    def __init__(self, status: int, code: str) -> None:
        self.status = status
        self.code = code

    def generate(self, **_kwargs):
        raise LLMAdapterError(self.code, delivery="sent", status_code=self.status)


class UnexpectedErrorProvider:
    def generate(self, **_kwargs):
        raise RuntimeError("unexpected provider failure")

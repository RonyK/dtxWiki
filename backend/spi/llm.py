"""Public contract for LLM text-generation adapters.

An adapter implements one provider API specification, such as Anthropic
Messages or Gemini generateContent. Core owns everything around the call:
which connection, credential, model and endpoint an execution uses, budgets,
retries and policy. An adapter turns one canonical request into one provider
call and normalizes the answer, its token usage and its errors.

Plugins are trusted in-process code. The contract limits what an adapter is
given (one execution's credential and an injected transport); it is not a
sandbox.
"""

from __future__ import annotations

import math
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Literal, Protocol, get_args

LLM_ADAPTER_SPI_VERSION = "1.0"

# Provider-neutral generation options a request may set. An option reaches the
# provider only when the request sets it and the model supports it; adapters
# never send one by default.
COMMON_OPTIONS: frozenset[str] = frozenset({"temperature"})

Termination = Literal[
    "completed", "truncated", "refused", "blocked", "tool_request", "unknown"
]
# Whether the provider received the request: billing and replay decisions
# depend on it. "sent" means the provider answered, even with an error.
Delivery = Literal["not_sent", "sent", "unknown"]
LLMErrorCode = Literal[
    "invalid_request",
    "authentication_failed",
    "permission_denied",
    "not_found",
    # Quota, spend limit or billing refuses the call: waiting does not help.
    "quota_exhausted",
    "rate_limited",
    "provider_unavailable",
    "timeout",
    "network_error",
    "invalid_response",
    "provider_error",
    "unsupported_option",
]

_TERMINATIONS = frozenset(get_args(Termination))
_DELIVERIES = frozenset(get_args(Delivery))
_ERROR_CODES = frozenset(get_args(LLMErrorCode))


def _optional_count(value: int | None, name: str) -> None:
    if value is not None and (type(value) is not int or value < 0):
        raise ValueError(f"{name} must be a non-negative integer or None")


@dataclass(frozen=True, slots=True)
class TextMessage:
    role: Literal["user", "assistant"]
    text: str

    def __post_init__(self) -> None:
        if self.role not in ("user", "assistant"):
            raise ValueError("message role must be user or assistant")
        if not isinstance(self.text, str):
            raise TypeError("message text must be a string")


@dataclass(frozen=True, slots=True)
class CanonicalTextRequest:
    """One text generation, independent of the provider's wire format."""

    model: str
    messages: tuple[TextMessage, ...]
    max_output_tokens: int
    system: str | None = None
    temperature: float | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "messages", tuple(self.messages))
        if not isinstance(self.model, str) or not self.model.strip():
            raise ValueError("model is required")
        if not self.messages or not all(
            isinstance(message, TextMessage) for message in self.messages
        ):
            raise ValueError("at least one TextMessage is required")
        if type(self.max_output_tokens) is not int or self.max_output_tokens < 1:
            raise ValueError("max_output_tokens must be a positive integer")
        if self.system is not None and not isinstance(self.system, str):
            raise TypeError("system must be a string")
        if self.temperature is not None and (
            isinstance(self.temperature, bool)
            or not isinstance(self.temperature, (int, float))
            or not math.isfinite(self.temperature)
        ):
            raise ValueError("temperature must be a finite number")

    @property
    def options(self) -> frozenset[str]:
        """The common options this request sets explicitly."""

        return frozenset({"temperature"} if self.temperature is not None else ())


@dataclass(frozen=True, slots=True)
class TokenUsage:
    """Provider-reported token counts.

    ``input_tokens`` includes both cache counts and ``output_tokens`` includes
    ``reasoning_output_tokens``, so input plus output never counts a token
    twice. ``None`` means the provider did not report the count; it is
    unknown, never zero.
    """

    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_input_tokens: int | None = None
    cache_write_input_tokens: int | None = None
    reasoning_output_tokens: int | None = None

    def __post_init__(self) -> None:
        _optional_count(self.input_tokens, "input_tokens")
        _optional_count(self.output_tokens, "output_tokens")
        _optional_count(self.cache_read_input_tokens, "cache_read_input_tokens")
        _optional_count(self.cache_write_input_tokens, "cache_write_input_tokens")
        _optional_count(self.reasoning_output_tokens, "reasoning_output_tokens")


@dataclass(frozen=True, slots=True)
class CanonicalTextResult:
    """A provider answer. Only ``completed`` text is a finished answer."""

    text: str
    termination: Termination
    model: str | None = None
    usage: TokenUsage = field(default_factory=TokenUsage)
    provider_request_id: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.text, str):
            raise TypeError("result text must be a string")
        if self.termination not in _TERMINATIONS:
            raise ValueError("unknown termination")


class LLMAdapterError(RuntimeError):
    """A normalized failure. The message is the stable code only.

    Provider error text can echo prompts or account details, so it never
    reaches the exception, logs or users.
    """

    def __init__(
        self,
        code: LLMErrorCode,
        *,
        delivery: Delivery,
        status_code: int | None = None,
        retry_after_seconds: float | None = None,
        provider_request_id: str | None = None,
    ) -> None:
        if code not in _ERROR_CODES:
            raise ValueError("unknown LLM error code")
        if delivery not in _DELIVERIES:
            raise ValueError("unknown delivery")
        super().__init__(code)
        self.code = code
        self.delivery = delivery
        self.status_code = status_code
        self.retry_after_seconds = retry_after_seconds
        self.provider_request_id = provider_request_id


@dataclass(frozen=True, slots=True)
class ExecutionContext:
    """Limits for one generation.

    ``timeout_seconds`` bounds each network operation (connect, write, every
    read), not the whole call: a provider that keeps sending can run longer.
    """

    timeout_seconds: float

    def __post_init__(self) -> None:
        if (
            isinstance(self.timeout_seconds, bool)
            or not isinstance(self.timeout_seconds, (int, float))
            or not math.isfinite(self.timeout_seconds)
            or self.timeout_seconds <= 0
        ):
            raise ValueError("timeout_seconds must be a positive finite number")


@dataclass(frozen=True, slots=True)
class LLMModelCapabilities:
    """What one model accepts. Unknown limits are ``None``."""

    max_output_tokens: int | None = None
    options: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        if self.max_output_tokens is not None and (
            type(self.max_output_tokens) is not int or self.max_output_tokens < 1
        ):
            raise ValueError("max_output_tokens must be a positive integer or None")
        object.__setattr__(self, "options", frozenset(self.options))
        if not self.options <= COMMON_OPTIONS:
            raise ValueError("capabilities name an unknown option")

    def require_supported(self, request: CanonicalTextRequest) -> None:
        """Reject, before anything is sent, what the model cannot honor.

        An unsupported option is never renamed or silently dropped.
        """

        if not request.options <= self.options:
            raise LLMAdapterError("unsupported_option", delivery="not_sent")
        if (
            self.max_output_tokens is not None
            and request.max_output_tokens > self.max_output_tokens
        ):
            raise LLMAdapterError("invalid_request", delivery="not_sent")


@dataclass(frozen=True, slots=True)
class TransportResponse:
    status_code: int
    # Provider data can echo the request; keep it out of reprs and logs.
    headers: Mapping[str, str] = field(repr=False)
    body: bytes = field(repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "headers",
            MappingProxyType(
                {str(name).lower(): str(value) for name, value in self.headers.items()}
            ),
        )


class SSEResponse(Protocol):
    """An open HTTP reply read as it arrives (an ``httpx.Response``)."""

    status_code: int
    headers: Mapping[str, str]

    @property
    def is_success(self) -> bool: ...

    def aiter_bytes(self) -> AsyncIterator[bytes]: ...


class LLMTransport(Protocol):
    """Core-owned network access: endpoint policy, proxy and TLS live here.

    One call is one POST of ``payload`` (JSON-compatible values). A failure
    raises :class:`LLMAdapterError` with the delivery the transport can prove;
    a URL outside the execution's connection is refused before sending
    (``invalid_request``).
    """

    async def post_json(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        payload: Mapping[str, object],
        timeout_seconds: float,
    ) -> TransportResponse:
        """Return any HTTP reply, fully read."""
        ...

    def open_sse(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        payload: Mapping[str, object],
        timeout_seconds: float,
    ) -> AbstractAsyncContextManager[SSEResponse]:
        """Open a Server-Sent Events reply for a streaming port to read."""
        ...


@dataclass(frozen=True, slots=True)
class LLMConnection:
    """One execution's resolved connection.

    ``base_url`` is resolved and checked by Core. ``config`` and
    ``credential`` have passed the descriptor's schemas.
    """

    base_url: str
    config: Mapping[str, object] = field(default_factory=dict)
    credential: Mapping[str, str] = field(default_factory=dict, repr=False)


@dataclass(frozen=True, slots=True)
class GenerationFeatures:
    """What a port does besides one ``generate`` call.

    ``streaming``: ``stream`` passes the answer's text on as it arrives.
    ``steering``: ``stream_with_steering`` also takes instructions while the
    model answers; it builds on streaming.
    """

    streaming: bool
    steering: bool

    def __post_init__(self) -> None:
        if type(self.streaming) is not bool or type(self.steering) is not bool:
            raise TypeError("generation features must be booleans")
        if self.steering and not self.streaming:
            raise ValueError("steering requires streaming")

    def within(self, other: GenerationFeatures) -> bool:
        return (not self.streaming or other.streaming) and (
            not self.steering or other.steering
        )


@dataclass(frozen=True, slots=True)
class LLMAdapterDescriptor:
    """Identity, input schemas and features of one API specification.

    ``adapter_id`` is namespaced (``builtin.*`` or ``<plugin name>.*``) and
    never changes protocol. ``features`` declares what the adapter
    implements; a port may offer less for its model and endpoint, never more.
    ``options`` lists the common options the adapter can encode. Both schemas
    are self-contained JSON Schema 2020-12 documents (no ``$schema``,
    references, ``$id`` or anchors); ``credential_schema`` declares input-only
    secret fields as a flat object with ``properties``, optional ``required``,
    and ``additionalProperties: false``.
    """

    adapter_id: str
    adapter_version: str
    display_name: Mapping[str, str]
    config_schema: Mapping[str, object]
    credential_schema: Mapping[str, object]
    features: GenerationFeatures
    options: frozenset[str] = frozenset()
    default_base_url: str | None = None
    config_schema_version: int = 1
    spi_version: str = LLM_ADAPTER_SPI_VERSION


@dataclass(frozen=True, slots=True)
class SteerInput:
    """An instruction for the answer being written."""

    id: str
    text: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class SteerUpdate:
    """What became of an instruction. ``sent`` means the provider has it;
    ``applied``, ``failed`` and ``unknown`` are final."""

    id: str
    status: Literal["sent", "accepted", "applied", "failed", "unknown"]
    error_code: str | None = None


OnDelta = Callable[[str], Awaitable[None]]
NextSteer = Callable[[], Awaitable[SteerInput]]
OnSteer = Callable[[SteerUpdate], Awaitable[None]]


class TextGenerationPort(Protocol):
    """One execution's adapter for one model.

    ``features`` says which of ``stream`` and ``stream_with_steering`` the
    port offers; Core calls no other. Each of the three makes at most one
    provider call: no hidden retry or continuation.
    """

    @property
    def features(self) -> GenerationFeatures: ...

    async def generate(
        self, request: CanonicalTextRequest, context: ExecutionContext
    ) -> CanonicalTextResult: ...


class StreamingTextPort(TextGenerationPort, Protocol):
    async def stream(
        self,
        request: CanonicalTextRequest,
        context: ExecutionContext,
        *,
        on_delta: OnDelta,
    ) -> CanonicalTextResult:
        """Generate, passing each piece of the answer's text to ``on_delta``."""
        ...


class SteerableTextPort(StreamingTextPort, Protocol):
    async def stream_with_steering(
        self,
        request: CanonicalTextRequest,
        context: ExecutionContext,
        *,
        on_delta: OnDelta,
        next_steer: NextSteer,
        on_steer: OnSteer,
    ) -> CanonicalTextResult:
        """Stream, and send the provider each instruction while it answers.

        ``next_steer`` returns the oldest instruction not yet sent; it stays
        queued until ``on_steer`` reports it, ``sent`` or already settled.
        Every instruction the port takes ends ``applied``, ``failed`` or
        ``unknown``. An applied
        instruction starts a new response: ``on_delta`` then carries the new
        text, which replaces what was passed before.
        """
        ...


class LLMAdapterFactory(Protocol):
    @property
    def descriptor(self) -> LLMAdapterDescriptor: ...

    def create(
        self, connection: LLMConnection, transport: LLMTransport, model: str
    ) -> TextGenerationPort:
        """Build the adapter for one execution of ``model``."""
        ...


__all__ = [
    "COMMON_OPTIONS",
    "LLM_ADAPTER_SPI_VERSION",
    "CanonicalTextRequest",
    "CanonicalTextResult",
    "Delivery",
    "ExecutionContext",
    "GenerationFeatures",
    "LLMAdapterDescriptor",
    "LLMAdapterError",
    "LLMAdapterFactory",
    "LLMConnection",
    "LLMErrorCode",
    "LLMModelCapabilities",
    "LLMTransport",
    "NextSteer",
    "OnDelta",
    "OnSteer",
    "SSEResponse",
    "SteerInput",
    "SteerUpdate",
    "SteerableTextPort",
    "StreamingTextPort",
    "Termination",
    "TextGenerationPort",
    "TextMessage",
    "TokenUsage",
    "TransportResponse",
]

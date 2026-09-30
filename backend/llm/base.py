from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Literal

MessageRole = Literal["system", "user", "assistant", "tool"]


@dataclass(frozen=True)
class LLMToolCall:
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)
    call_id: str | None = None


@dataclass(frozen=True)
class LLMMessage:
    role: MessageRole
    content: str
    name: str | None = None
    tool_call_id: str | None = None
    tool_calls: list[LLMToolCall] = field(default_factory=list)


@dataclass(frozen=True)
class LLMRequest:
    messages: list[LLMMessage]
    tools: list[dict[str, Any]] = field(default_factory=list)
    model: str | None = None
    temperature: float = 0.0
    max_tokens: int = 1024
    timeout: float | None = None


@dataclass(frozen=True)
class LLMResponse:
    provider: str
    model: str
    content: str
    tool_calls: list[LLMToolCall] = field(default_factory=list)
    stop_reason: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def truncated(self) -> bool:
        """True when generation stopped because the max_tokens budget ran out
        (Anthropic ``max_tokens``, OpenAI ``length``) rather than reaching a
        natural end of turn - the content must not be presented as a complete
        answer."""
        return self.stop_reason in {"max_tokens", "length"}


@dataclass(frozen=True)
class EmbeddingRequest:
    texts: list[str]
    model: str | None = None


@dataclass(frozen=True)
class EmbeddingResponse:
    provider: str
    model: str
    dimensions: int
    embeddings: list[list[float]]


@dataclass(frozen=True)
class RerankRequest:
    query: str
    documents: list[str]
    top_n: int | None = None


@dataclass(frozen=True)
class RerankResult:
    # Index into the request's ``documents`` list, with the cross-encoder
    # relevance score (higher is more relevant).
    index: int
    score: float


@dataclass(frozen=True)
class RerankResponse:
    provider: str
    model: str
    # Best-first; each ``index`` points back into the request documents.
    results: list[RerankResult]


class LLMProviderError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        provider: str,
        status_code: int | None = None,
        error_type: str | None = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.provider = provider
        self.status_code = status_code
        self.error_type = error_type
        self.retryable = retryable


class LLMProvider(ABC):
    @abstractmethod
    def chat(self, request: LLMRequest) -> LLMResponse:
        raise NotImplementedError

    @abstractmethod
    async def async_chat(self, request: LLMRequest) -> LLMResponse:
        raise NotImplementedError


class EmbeddingProvider(ABC):
    @property
    @abstractmethod
    def dimensions(self) -> int:
        """Vector width this provider emits (must equal the stored index dim)."""
        raise NotImplementedError

    @abstractmethod
    def embed(self, request: EmbeddingRequest) -> EmbeddingResponse:
        raise NotImplementedError


class RerankProvider(ABC):
    @abstractmethod
    def rerank(self, request: RerankRequest) -> RerankResponse:
        """Score the request documents against the query, returning them
        best-first. dtxWiki connects to an external rerank endpoint; it does
        not run the cross-encoder in-process."""
        raise NotImplementedError

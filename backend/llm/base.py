from abc import ABC, abstractmethod
from dataclasses import dataclass


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

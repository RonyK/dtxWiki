import json

import httpx
import pytest

from backend.app.settings import Settings
from backend.llm.base import EmbeddingRequest
from backend.llm.embedding import (
    DeterministicEmbedding,
    OpenAICompatibleEmbedding,
)
from backend.llm.registry import assert_embedding_dim, build_embedding_provider
from backend.services.vector_store.bruteforce import cosine_similarity

pytestmark = pytest.mark.unit


def test_deterministic_embedding_is_stable_and_dim_correct():
    provider = DeterministicEmbedding(dim=64)
    assert provider.dimensions == 64
    first = provider.embed(EmbeddingRequest(texts=["alpha beta gamma"]))
    second = provider.embed(EmbeddingRequest(texts=["alpha beta gamma"]))
    assert first.dimensions == 64
    assert len(first.embeddings[0]) == 64
    assert first.embeddings[0] == second.embeddings[0]  # deterministic


def test_deterministic_embedding_overlap_is_more_similar():
    provider = DeterministicEmbedding(dim=256)
    vectors = provider.embed(
        EmbeddingRequest(
            texts=[
                "alpha beta gamma delta",
                "alpha beta gamma epsilon",
                "completely unrelated tokens here",
            ]
        )
    ).embeddings
    overlap = cosine_similarity(vectors[0], vectors[1])
    disjoint = cosine_similarity(vectors[0], vectors[2])
    assert overlap > disjoint


def test_openai_compatible_embedding_parses_response():
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["model"] == "test-model"
        count = len(body["input"])
        assert request.headers["Authorization"] == "Bearer secret"
        return httpx.Response(
            200, json={"data": [{"embedding": [0.1, 0.2, 0.3]} for _ in range(count)]}
        )

    provider = OpenAICompatibleEmbedding(
        base_url="http://embed.local/v1",
        model="test-model",
        dim=3,
        api_key="secret",
        transport=httpx.MockTransport(handler),
    )
    response = provider.embed(EmbeddingRequest(texts=["a", "b"]))
    assert response.dimensions == 3
    assert response.provider == "openai_compatible"
    assert response.embeddings == [[0.1, 0.2, 0.3], [0.1, 0.2, 0.3]]


def test_openai_compatible_embedding_batches_large_input():
    """M3: a document larger than batch_size must be split across requests, in
    order, instead of one oversized request that exceeds provider input caps."""
    requests_seen: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        requests_seen.append(len(body["input"]))
        return httpx.Response(
            200,
            json={
                "data": [
                    {"index": i, "embedding": [float(len(text)), 0.0, 0.0]}
                    for i, text in enumerate(body["input"])
                ]
            },
        )

    provider = OpenAICompatibleEmbedding(
        base_url="http://embed.local/v1",
        model="m",
        dim=3,
        transport=httpx.MockTransport(handler),
        batch_size=2,
    )
    texts = ["a", "bb", "ccc", "dddd", "eeeee"]
    response = provider.embed(EmbeddingRequest(texts=texts))
    assert requests_seen == [2, 2, 1]
    assert len(response.embeddings) == 5
    # Order preserved across batches (first component encodes len(text)).
    assert [e[0] for e in response.embeddings] == [1.0, 2.0, 3.0, 4.0, 5.0]


def test_openai_compatible_embedding_rejects_dim_mismatch():
    """M3/L7: a server returning the wrong width fails loudly, not silently."""

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        return httpx.Response(
            200, json={"data": [{"embedding": [0.1, 0.2]} for _ in body["input"]]}
        )

    provider = OpenAICompatibleEmbedding(
        base_url="http://embed.local/v1",
        model="m",
        dim=3,
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(RuntimeError, match="dim"):
        provider.embed(EmbeddingRequest(texts=["a"]))


def test_openai_compatible_embedding_retries_transient_error():
    """M3: a transient 5xx is retried rather than failing the whole index job."""
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        if attempts["n"] == 1:
            return httpx.Response(503, json={"error": "busy"})
        body = json.loads(request.content)
        return httpx.Response(
            200, json={"data": [{"embedding": [0.0, 0.0, 0.0]} for _ in body["input"]]}
        )

    provider = OpenAICompatibleEmbedding(
        base_url="http://embed.local/v1",
        model="m",
        dim=3,
        transport=httpx.MockTransport(handler),
        max_retries=2,
        sleep=lambda _seconds: None,
    )
    response = provider.embed(EmbeddingRequest(texts=["a"]))
    assert attempts["n"] == 2  # one retry
    assert len(response.embeddings) == 1


def test_build_embedding_provider_modes():
    assert build_embedding_provider(Settings()) is None
    assert build_embedding_provider(Settings(embedding_provider="none")) is None

    deterministic = build_embedding_provider(
        Settings(embedding_provider="deterministic", embedding_dim=128)
    )
    assert deterministic is not None and deterministic.dimensions == 128

    with pytest.raises(ValueError, match="EMBEDDING_BASE_URL"):
        build_embedding_provider(Settings(embedding_provider="openai_compatible"))

    with pytest.raises(ValueError, match="must be one of"):
        build_embedding_provider(Settings(embedding_provider="bogus"))


def test_assert_embedding_dim_guard():
    provider = DeterministicEmbedding(dim=768)
    assert_embedding_dim(provider, 768)  # ok
    assert_embedding_dim(None, 768)  # no provider -> no-op
    with pytest.raises(RuntimeError, match="reindex"):
        assert_embedding_dim(provider, 1536)

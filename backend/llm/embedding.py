"""Embedding providers — Epic-1 Sprint-1 Phase 4.

Two pluggable implementations behind ``EmbeddingProvider``:

* ``DeterministicEmbedding`` — dependency-zero hashing bag-of-words. Not a
  semantic model, but overlapping text maps to similar vectors, so the RAG
  pipeline (chunk -> embed -> cosine search) is exercisable offline and in
  tests without downloading a model. The safe default when no model/API is set.
* ``OpenAICompatibleEmbedding`` — POSTs the OpenAI ``/v1/embeddings`` shape to a
  configurable ``base_url``, so the same client targets OpenAI, Voyage, Gemini
  (compat), or a self-hosted server (TEI / Infinity / vLLM / Ollama). This is
  the path for a self-hosted/local model server.
"""

from __future__ import annotations

import hashlib
import re
import time
from collections.abc import Callable
from math import sqrt

import httpx

from backend.llm.base import (
    EmbeddingProvider,
    EmbeddingRequest,
    EmbeddingResponse,
)

_TOKEN_RE = re.compile(r"\w+", re.UNICODE)
# Transient HTTP statuses worth a retry; everything else fails fast.
_RETRYABLE_STATUS = {429, 500, 502, 503, 504}


def _tokenize(text: str) -> list[str]:
    return _TOKEN_RE.findall(text.lower())


class DeterministicEmbedding(EmbeddingProvider):
    def __init__(self, *, dim: int = 768, model: str = "hash-bow-v1") -> None:
        self._dim = dim
        self._model = model

    @property
    def dimensions(self) -> int:
        return self._dim

    def embed(self, request: EmbeddingRequest) -> EmbeddingResponse:
        return EmbeddingResponse(
            provider="deterministic",
            model=self._model,
            dimensions=self._dim,
            embeddings=[self._embed_one(text) for text in request.texts],
        )

    def _embed_one(self, text: str) -> list[float]:
        vector = [0.0] * self._dim
        for token in _tokenize(text):
            digest = hashlib.sha1(token.encode("utf-8")).hexdigest()
            vector[int(digest, 16) % self._dim] += 1.0
        norm = sqrt(sum(value * value for value in vector))
        if norm > 0.0:
            vector = [value / norm for value in vector]
        return vector


class OpenAICompatibleEmbedding(EmbeddingProvider):
    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        dim: int,
        api_key: str | None = None,
        transport: httpx.BaseTransport | None = None,
        timeout: float = 30.0,
        batch_size: int = 64,
        max_retries: int = 2,
        backoff_seconds: float = 0.5,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        self._model = model
        self._dim = dim
        self._api_key = api_key
        # Cap the per-request input array so a large document (hundreds of
        # chunks) does not exceed provider input limits (OpenAI ~2048; many
        # self-hosted servers far lower) and fail the whole index job.
        self._batch_size = max(1, batch_size)
        self._max_retries = max(0, max_retries)
        self._backoff_seconds = backoff_seconds
        self._sleep = sleep or time.sleep
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            timeout=timeout,
            transport=transport,
        )

    @property
    def dimensions(self) -> int:
        return self._dim

    def embed(self, request: EmbeddingRequest) -> EmbeddingResponse:
        model = request.model or self._model
        embeddings: list[list[float]] = []
        for start in range(0, len(request.texts), self._batch_size):
            batch = request.texts[start : start + self._batch_size]
            embeddings.extend(self._embed_batch(batch, model=model))
        return EmbeddingResponse(
            provider="openai_compatible",
            model=self._model,
            dimensions=self._dim,
            embeddings=embeddings,
        )

    def _embed_batch(self, texts: list[str], *, model: str) -> list[list[float]]:
        if not texts:
            return []
        payload = self._post_with_retry({"model": model, "input": texts})
        # Order by the response index when present (OpenAI spec), so the vectors
        # line up with the inputs regardless of server return order.
        items = sorted(payload["data"], key=lambda item: item.get("index", 0))
        if len(items) != len(texts):
            raise RuntimeError(
                f"embedding provider returned {len(items)} vectors for "
                f"{len(texts)} inputs"
            )
        vectors: list[list[float]] = []
        for item in items:
            vector = item["embedding"]
            if len(vector) != self._dim:
                raise RuntimeError(
                    f"embedding provider returned dim {len(vector)} != configured "
                    f"{self._dim}; reindex after changing the embedding model"
                )
            vectors.append(vector)
        return vectors

    def _post_with_retry(self, json_body: dict) -> dict:
        headers = {}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        attempt = 0
        while True:
            try:
                response = self._client.post(
                    "/embeddings", json=json_body, headers=headers
                )
                response.raise_for_status()
                return response.json()
            except httpx.HTTPStatusError as exc:
                if (
                    exc.response.status_code not in _RETRYABLE_STATUS
                    or attempt >= self._max_retries
                ):
                    raise
            except httpx.TransportError:
                if attempt >= self._max_retries:
                    raise
            attempt += 1
            self._sleep(self._backoff_seconds * (2 ** (attempt - 1)))

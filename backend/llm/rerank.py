"""Reranker providers — Epic-1 Sprint-2 Phase C.

``ApiReranker`` POSTs to an external cross-encoder rerank endpoint and returns
the documents best-first. dtxWiki only connects to the endpoint (operator runs
TEI / Infinity / vLLM ``/rerank``, or a Cohere/Voyage rerank API); it never runs
the model in-process — the same dependency-zero principle as embeddings.

Rerank responses are less standardized than the OpenAI embeddings shape, so the
parser accepts the two common forms:

* Cohere / vLLM / Jina / Voyage: ``{"results": [{"index": i, "relevance_score": s}]}``
* TEI / Infinity: a top-level list ``[{"index": i, "score": s}]``
"""

from __future__ import annotations

import time
from collections.abc import Callable

import httpx

from backend.llm.base import (
    RerankProvider,
    RerankRequest,
    RerankResponse,
    RerankResult,
)

# Transient HTTP statuses worth a retry; everything else fails fast.
_RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class ApiReranker(RerankProvider):
    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        api_key: str | None = None,
        transport: httpx.BaseTransport | None = None,
        timeout: float = 30.0,
        max_retries: int = 2,
        backoff_seconds: float = 0.5,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        self._model = model
        self._api_key = api_key
        self._max_retries = max(0, max_retries)
        self._backoff_seconds = backoff_seconds
        self._sleep = sleep or time.sleep
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            timeout=timeout,
            transport=transport,
        )

    def rerank(self, request: RerankRequest) -> RerankResponse:
        if not request.documents:
            return RerankResponse(provider="api", model=self._model, results=[])
        body: dict = {
            "model": self._model,
            "query": request.query,
            "documents": request.documents,
        }
        if request.top_n is not None:
            body["top_n"] = request.top_n
        payload = self._post_with_retry(body)
        results = self._parse(payload, document_count=len(request.documents))
        return RerankResponse(provider="api", model=self._model, results=results)

    def _parse(self, payload: object, *, document_count: int) -> list[RerankResult]:
        if isinstance(payload, dict) and "results" in payload:
            rows = payload["results"]
        elif isinstance(payload, list):
            rows = payload
        else:
            raise RuntimeError(
                "unrecognized rerank response shape: expected a list or an object "
                "with a 'results' field"
            )
        results: list[RerankResult] = []
        for row in rows:
            index = int(row["index"])
            score = row.get("relevance_score", row.get("score"))
            if score is None:
                raise RuntimeError(
                    "rerank response item missing 'relevance_score'/'score'"
                )
            if not 0 <= index < document_count:
                raise RuntimeError(
                    f"rerank index {index} out of range for {document_count} "
                    "documents"
                )
            results.append(RerankResult(index=index, score=float(score)))
        # Normalize to best-first regardless of server return order.
        results.sort(key=lambda result: -result.score)
        return results

    def _post_with_retry(self, json_body: dict) -> object:
        headers = {}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        attempt = 0
        while True:
            try:
                response = self._client.post(
                    "/rerank", json=json_body, headers=headers
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

from uuid import uuid4

import httpx
import pytest

from backend.app.settings import Settings
from backend.llm.base import (
    RerankRequest,
    RerankResponse,
    RerankResult,
)
from backend.llm.registry import build_reranker
from backend.llm.rerank import ApiReranker
from backend.schemas.read import RankedResult
from backend.services.search_service import SearchService


def _reranker(handler) -> ApiReranker:
    return ApiReranker(
        base_url="http://rerank.test/v1",
        model="rerank-test",
        transport=httpx.MockTransport(handler),
        sleep=lambda _seconds: None,
    )


# --- ApiReranker HTTP client ------------------------------------------------


def test_api_reranker_results_object_shape_sorted_best_first():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/rerank"
        body = request.read().decode()
        assert "rerank-test" in body
        return httpx.Response(
            200,
            json={
                "results": [
                    {"index": 0, "relevance_score": 0.2},
                    {"index": 2, "relevance_score": 0.9},
                    {"index": 1, "relevance_score": 0.5},
                ]
            },
        )

    response = _reranker(handler).rerank(
        RerankRequest(query="q", documents=["a", "b", "c"], top_n=3)
    )
    assert [result.index for result in response.results] == [2, 1, 0]
    assert response.results[0].score == 0.9


def test_api_reranker_accepts_tei_list_shape():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json=[{"index": 1, "score": 0.8}, {"index": 0, "score": 0.1}]
        )

    response = _reranker(handler).rerank(RerankRequest(query="q", documents=["a", "b"]))
    assert [result.index for result in response.results] == [1, 0]


def test_api_reranker_rejects_out_of_range_index():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"results": [{"index": 5, "relevance_score": 1.0}]}
        )

    with pytest.raises(RuntimeError):
        _reranker(handler).rerank(RerankRequest(query="q", documents=["a", "b"]))


def test_api_reranker_empty_documents_skips_http_call():
    def handler(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("should not POST for empty documents")

    response = _reranker(handler).rerank(RerankRequest(query="q", documents=[]))
    assert response.results == []


# --- registry ---------------------------------------------------------------


def test_build_reranker_modes():
    assert build_reranker(Settings()) is None
    with pytest.raises(ValueError):
        build_reranker(Settings(rerank_provider="api"))  # base_url required
    reranker = build_reranker(
        Settings(
            rerank_provider="api",
            rerank_base_url="http://rerank.test/v1",
            rerank_model="m",
        )
    )
    assert isinstance(reranker, ApiReranker)


# --- SearchService rerank stage ---------------------------------------------


def _result(title: str, score: float) -> RankedResult:
    return RankedResult(
        id=uuid4(),
        type="context_page",
        title=title,
        snippet="",
        score=score,
        citation_available=False,
    )


class _FakeRetriever:
    def __init__(self, results: list[RankedResult]) -> None:
        self._results = results

    def supported_modes(self) -> set[str]:
        return {"keyword"}

    def retrieve(self, *, session, workspace_id, query, scope, filters, limit, actor):
        return list(self._results)[:limit]


class _FakeReranker:
    """Returns the documents at ``order`` indices (best-first), simulating an
    endpoint that may also truncate to top_n by returning fewer than all."""

    def __init__(self, order: list[int], *, fail: bool = False) -> None:
        self._order = order
        self._fail = fail

    def rerank(self, request: RerankRequest) -> RerankResponse:
        if self._fail:
            raise RuntimeError("rerank endpoint unreachable")
        order = [index for index in self._order if index < len(request.documents)]
        results = [
            RerankResult(index=index, score=float(len(order) - rank))
            for rank, index in enumerate(order)
        ]
        return RerankResponse(provider="fake", model="fake", results=results)


def _paths(payload) -> list[str]:
    return [result.title for result in payload.ranked_results]


def _search(service: SearchService):
    return service.search(
        session=None,
        workspace_id=uuid4(),
        query="Query",
        retrieval_mode="keyword",
        limit=10,
    )


def test_search_reranks_candidate_order():
    results = [_result("a", 0.9), _result("b", 0.8), _result("c", 0.7)]
    service = SearchService(
        retrievers=[_FakeRetriever(results)],
        reranker=_FakeReranker(order=[2, 0, 1]),
    )
    assert _paths(_search(service)) == ["c", "a", "b"]


def test_search_without_reranker_keeps_fusion_order():
    # Given out of score order; the dispatcher sorts by score desc.
    service = SearchService(
        retrievers=[_FakeRetriever([_result("b", 0.8), _result("a", 0.9)])]
    )
    assert _paths(_search(service)) == ["a", "b"]

    from backend.services.retrieval.fusion import (
        ranked_result_key,
        reciprocal_rank_fusion,
    )

    # Audit source refs are navigation targets, so separate feedback on one
    # page must retain separate identities when retrieval lanes are fused.
    target_id = uuid4()
    feedback = [
        _result("first", 0.9).model_copy(
            update={
                "type": "feedback",
                "source_ref": {"kind": "context_page", "id": str(target_id)},
            }
        ),
        _result("second", 0.8).model_copy(
            update={
                "type": "feedback",
                "source_ref": {"kind": "context_page", "id": str(target_id)},
            }
        ),
    ]
    fused = reciprocal_rank_fusion([feedback, []], key=ranked_result_key)
    assert [result.id for result in fused] == [result.id for result in feedback]
    from backend.services.eval.harness import result_identifier

    assert result_identifier(feedback[0]) == f"feedback:{feedback[0].id}"

    # Document-version identity is independent of the audit-target rule above:
    # each version survives, while its keyword and chunk hits collapse together.
    published_id, draft_id = uuid4(), uuid4()
    versions = [
        _result("Published", 0.9).model_copy(
            update={"id": target_id, "version_id": published_id}
        ),
        _result("Draft", 0.8).model_copy(
            update={"id": target_id, "version_id": draft_id}
        ),
    ]
    chunks = [
        result.model_copy(
            update={
                "id": uuid4(),
                "source_ref": {
                    "kind": "content_chunk",
                    "document_kind": "context_page",
                    "document_id": str(target_id),
                    "version_id": str(result.version_id),
                },
            }
        )
        for result in versions
    ]
    # Legacy chunk-shaped providers may carry the version only in source_ref.
    chunks[0].version_id = None
    fused = reciprocal_rank_fusion([versions, chunks], key=ranked_result_key)
    assert [(result.id, result.version_id) for result in fused] == [
        (target_id, published_id),
        (target_id, draft_id),
    ]
    # Golden retrieval quality is measured by document, not by its version rows.
    from backend.services.eval.harness import _predicted_identifiers

    assert _predicted_identifiers(
        SearchService(retrievers=[_FakeRetriever(fused)]),
        None,
        None,
        query="versioned document quality",
        mode="keyword",
        k=2,
        actor=None,
    ) == [f"context_page:{target_id}"]
    # Result composition keeps claims as distinct items while deduplicating
    # page/chunk representatives of the same document version.
    from backend.services.search_service import _merge_ranked_results

    claim = chunks[0].model_copy(
        update={
            "type": "context_claim",
            "version_id": published_id,
            "source_ref": {"kind": "context_page", "id": str(target_id)},
        }
    )
    merged = _merge_ranked_results(primary=[versions[0]], fallback=[claim, *chunks])
    assert [(result.id, result.version_id) for result in merged] == [
        (target_id, published_id),
        (claim.id, published_id),
        (chunks[1].id, draft_id),
    ]


def test_search_rerank_failure_falls_back_to_fusion_order():
    service = SearchService(
        retrievers=[_FakeRetriever([_result("a", 0.9), _result("b", 0.8)])],
        reranker=_FakeReranker(order=[1, 0], fail=True),
    )
    assert _paths(_search(service)) == ["a", "b"]


def test_search_rerank_top_n_truncation_keeps_all_results():
    results = [
        _result("a", 0.9),
        _result("b", 0.8),
        _result("c", 0.7),
        _result("d", 0.6),
    ]
    service = SearchService(
        retrievers=[_FakeRetriever(results)],
        reranker=_FakeReranker(order=[3, 2]),  # endpoint returns only its top-2
        rerank_top_n=2,
    )
    # reranked [d, c] first, then the dropped candidates in fusion order [a, b].
    assert _paths(_search(service)) == ["d", "c", "a", "b"]

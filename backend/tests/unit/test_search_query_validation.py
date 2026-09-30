from uuid import uuid4

import pytest
from pydantic import ValidationError

from backend.schemas.mcp.read_tools import SearchInput
from backend.schemas.read import SEARCH_QUERY_MAX_LENGTH, SearchFilters, SearchRequest
from backend.services.retrieval.keyword import KeywordRetriever

pytestmark = pytest.mark.unit


def test_rest_search_query_accepts_limit_boundary():
    request = SearchRequest(query="x" * SEARCH_QUERY_MAX_LENGTH)

    assert len(request.query) == SEARCH_QUERY_MAX_LENGTH


def test_rest_search_query_rejects_over_limit():
    with pytest.raises(ValidationError):
        SearchRequest(query="x" * (SEARCH_QUERY_MAX_LENGTH + 1))


def test_mcp_search_query_rejects_over_limit():
    with pytest.raises(ValidationError):
        SearchInput(
            workspace_id=uuid4(),
            query="x" * (SEARCH_QUERY_MAX_LENGTH + 1),
        )


def test_rest_search_scope_rejects_unknown_value():
    with pytest.raises(ValidationError):
        SearchRequest(query="rollback", scope="everything")


@pytest.mark.parametrize("query", ["", "   ", "\t\r\n"])
def test_keyword_retriever_short_circuits_blank_query(query):
    results = KeywordRetriever().retrieve(
        session=None,  # type: ignore[arg-type] - the blank-query guard precedes DB access.
        workspace_id=uuid4(),
        query=query,
        scope="all",
        filters=SearchFilters(),
        limit=20,
        actor=None,
    )

    assert results == []

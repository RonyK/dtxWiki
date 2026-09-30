"""Non-SQLite data planes degrade local FTS5 lanes without false misses."""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from backend.app.container import (
    DatabaseProvider,
    ServiceRegistry,
    ToolCallContext,
    assemble,
)
from backend.app.main import create_app
from backend.app.settings import Settings
from backend.mcp.read_tools import search as mcp_search
from backend.models.coverage import KnowledgeCoverageGap
from backend.models.workspace import Workspace
from backend.policy.auth import ActorContext
from backend.schemas.ask import AskSessionCreate, AskTurnCreate
from backend.schemas.read import RankedResult, SearchFilters
from backend.services.ask_service import AskService
from backend.services.retrieval.keyword import (
    KeywordRetriever,
    _warn_symbolic_lane_unavailable,
)
from backend.services.search_service import (
    KEYWORD_LANE_UNAVAILABLE_WARNING,
    SearchService,
)
from backend.tests.conftest import authenticate_test_client_in_workspace

pytestmark = pytest.mark.integration

_DEGRADATION_MESSAGE = (
    "Tier-B FTS5 symbolic lanes are disabled for postgresql; exact/direct "
    "lanes remain active — configure a search-plane Retriever for this database."
)


@pytest.fixture(autouse=True)
def _reset_symbolic_warning_cache():
    _warn_symbolic_lane_unavailable.cache_clear()
    yield
    _warn_symbolic_lane_unavailable.cache_clear()


def _workspace_id(session_factory):
    with session_factory() as session:
        workspace_id = session.scalar(
            select(Workspace.id).where(Workspace.slug == "default")
        )
    assert workspace_id is not None
    return workspace_id


def _set_postgresql_dialect(monkeypatch, session_factory):
    engine = session_factory.kw["bind"]
    monkeypatch.setattr(engine.dialect, "name", "postgresql")


def _owner(workspace_id):
    return ActorContext(
        actor_kind="human",
        actor_id=str(uuid4()),
        workspace_id=workspace_id,
        workspace_role="owner",
        role="owner",
        token_kind="session",
    )


class _NoSqlSession:
    def get_bind(self):
        return SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))

    def execute(self, *_args, **_kwargs):
        raise AssertionError("SQLite FTS/trigram SQL reached a non-SQLite plane")


class _RemoteKeywordRetriever:
    def __init__(self):
        self.calls = 0

    def supported_modes(self):
        return {"keyword"}

    def retrieve(self, **_kwargs):
        self.calls += 1
        return []


class _DuckSearchService:
    def supported_modes(self):
        return ["keyword"]


def test_non_sqlite_guards_skip_raw_sql_but_keep_workflow_lanes(monkeypatch):
    session = _NoSqlSession()
    workspace_id = uuid4()
    workflow_result = RankedResult(
        id=uuid4(),
        type="feedback",
        title="Workflow feedback",
        snippet="workflow survives",
        score=1.0,
        citation_available=False,
        match_reason="keyword",
    )
    retriever = KeywordRetriever()
    monkeypatch.setattr(
        retriever,
        "_search_feedback",
        lambda **_kwargs: [workflow_result],
    )
    monkeypatch.setattr(retriever, "_search_changes", lambda **_kwargs: [])

    anonymous_results = retriever.retrieve(
        session=session,
        workspace_id=workspace_id,
        query="workflow",
        scope="all",
        filters=SearchFilters(),
        limit=5,
        actor=None,
    )
    authorized_results = retriever.retrieve(
        session=session,
        workspace_id=workspace_id,
        query="workflow",
        scope="all",
        filters=SearchFilters(),
        limit=5,
        actor=_owner(workspace_id),
    )
    trigram = SearchService()._trigram_lane(
        session=session,
        workspace_id=uuid4(),
        normalized_query="workflow",
        filters=SearchFilters(),
        actor=None,
        limit=5,
    )

    assert anonymous_results == []
    assert authorized_results == [workflow_result]
    assert trigram == []


def test_non_sqlite_search_preserves_exact_lane_and_warns_once(
    session_factory,
    page_service,
    monkeypatch,
):
    workspace_id = _workspace_id(session_factory)
    page_service.write_page(
        workspace_slug="default",
        slug="postgres-exact",
        title="Postgres Exact",
        body_md="# Postgres Exact\n\nExact resolver evidence.",
        actor_kind="human",
        actor_id=str(uuid4()),
        actor_provenance="browser_session",
    )
    _set_postgresql_dialect(monkeypatch, session_factory)
    logged_messages: list[str] = []
    monkeypatch.setattr(
        "backend.services.retrieval.keyword.logger.warning",
        logged_messages.append,
    )
    service = SearchService()

    with session_factory() as session:
        exact = service.search(
            session=session,
            workspace_id=workspace_id,
            query="Postgres Exact",
            actor=_owner(workspace_id),
        )
        missing = service.search(
            session=session,
            workspace_id=workspace_id,
            query="missing fuzzy phrase",
            actor=_owner(workspace_id),
        )

    assert exact.ranked_results[0].match_reason == "exact_title"
    assert exact.warnings == [KEYWORD_LANE_UNAVAILABLE_WARNING]
    assert missing.ranked_results == []
    assert missing.warnings == [KEYWORD_LANE_UNAVAILABLE_WARNING]
    assert logged_messages == [_DEGRADATION_MESSAGE]


def test_rest_search_and_capabilities_warn_without_recording_gap(
    session_factory,
    page_service,
    monkeypatch,
):
    settings = Settings(
        database_url="sqlite+pysqlite:///:memory:",
        session_secret="non-sqlite-rest-secret",
    )
    app = create_app(
        settings=settings,
        page_service=page_service,
        bootstrap_on_startup=False,
    )
    client = TestClient(app)
    authenticate_test_client_in_workspace(
        client,
        session_factory,
        email="non-sqlite-rest@example.com",
    )
    workspace_id = _workspace_id(session_factory)
    _set_postgresql_dialect(monkeypatch, session_factory)

    response = client.post(
        f"/api/v1/workspaces/{workspace_id}/search",
        json={"query": "missing postgres evidence"},
    )
    capabilities = client.get(
        f"/api/v1/workspaces/{workspace_id}/search/capabilities"
    )

    assert response.status_code == 200, response.text
    assert response.json()["data"]["warnings"] == [
        KEYWORD_LANE_UNAVAILABLE_WARNING
    ]
    assert [item["code"] for item in response.json()["envelope"]["warnings"]] == [
        KEYWORD_LANE_UNAVAILABLE_WARNING
    ]
    assert capabilities.status_code == 200, capabilities.text
    assert [
        item["code"] for item in capabilities.json()["envelope"]["warnings"]
    ] == [KEYWORD_LANE_UNAVAILABLE_WARNING]
    with session_factory() as session:
        assert session.scalar(select(func.count(KnowledgeCoverageGap.id))) == 0


def test_injected_keyword_retriever_avoids_false_dialect_warning_and_records_gap(
    session_factory,
    page_service,
    monkeypatch,
):
    workspace_id = _workspace_id(session_factory)
    _set_postgresql_dialect(monkeypatch, session_factory)
    remote = _RemoteKeywordRetriever()
    settings = Settings(
        database_url="sqlite+pysqlite:///:memory:",
        session_secret="remote-keyword-secret",
    )
    container = assemble(
        settings,
        session_factory=session_factory,
        page_service=page_service,
        search_service=SearchService(retrievers=[remote]),
    )
    app = create_app(container=container, bootstrap_on_startup=False)
    client = TestClient(app)
    authenticate_test_client_in_workspace(
        client,
        session_factory,
        email="remote-keyword@example.com",
    )

    response = client.post(
        f"/api/v1/workspaces/{workspace_id}/search",
        json={"query": "missing remote evidence"},
    )

    assert response.status_code == 200, response.text
    assert response.json()["data"]["warnings"] == ["coverage_gap_recorded"]
    assert remote.calls == 1
    with session_factory() as session:
        assert session.scalar(select(func.count(KnowledgeCoverageGap.id))) == 1


def test_capabilities_accepts_duck_search_service_without_diagnostics_method(
    session_factory,
    page_service,
):
    workspace_id = _workspace_id(session_factory)
    settings = Settings(
        database_url="sqlite+pysqlite:///:memory:",
        session_secret="duck-capabilities-secret",
    )
    container = assemble(
        settings,
        session_factory=session_factory,
        page_service=page_service,
        search_service=_DuckSearchService(),
    )
    app = create_app(container=container, bootstrap_on_startup=False)
    client = TestClient(app)
    authenticate_test_client_in_workspace(
        client,
        session_factory,
        email="duck-capabilities@example.com",
    )

    response = client.get(
        f"/api/v1/workspaces/{workspace_id}/search/capabilities"
    )

    assert response.status_code == 200, response.text
    assert response.json()["data"] == {
        "supported_retrieval_modes": ["keyword"]
    }
    assert response.json()["envelope"]["warnings"] == []


def test_mcp_search_warns_without_recording_gap(
    session_factory,
    page_service,
    monkeypatch,
):
    workspace_id = _workspace_id(session_factory)
    _set_postgresql_dialect(monkeypatch, session_factory)
    actor = ActorContext(
        actor_kind="mcp_client",
        actor_id="non-sqlite-mcp",
        scopes=("search", "workspace-read"),
        workspace_id=workspace_id,
        workspace_role="owner",
        role="owner",
        token_kind="mcp_client",
    )
    ctx = ToolCallContext(
        actor=actor,
        db=DatabaseProvider(None, session_factory=session_factory),
        page_service=page_service,
        search_service=SearchService(),
        services=ServiceRegistry(),
        workspace_id=workspace_id,
    )

    result = mcp_search(
        {
            "workspace_id": str(workspace_id),
            "query": "missing postgres evidence",
        },
        ctx,
    )

    assert result.data is not None
    assert result.data.warnings == [KEYWORD_LANE_UNAVAILABLE_WARNING]
    assert [warning.code for warning in result.envelope.warnings] == [
        KEYWORD_LANE_UNAVAILABLE_WARNING
    ]
    with session_factory() as session:
        assert session.scalar(select(func.count(KnowledgeCoverageGap.id))) == 0


def test_mcp_search_with_injected_keyword_retriever_records_gap(
    session_factory,
    page_service,
    monkeypatch,
):
    # MCP analog of the REST retriever pin: when a keyword-serving Retriever is
    # injected on a non-SQLite plane, the MCP search surface must NOT emit the
    # false keyword_lane_unavailable warning and MUST resume recording genuine
    # zero-hit coverage gaps (the warning-gated suppression re-enables).
    workspace_id = _workspace_id(session_factory)
    _set_postgresql_dialect(monkeypatch, session_factory)
    remote = _RemoteKeywordRetriever()
    actor = ActorContext(
        actor_kind="mcp_client",
        actor_id="non-sqlite-mcp-retriever",
        scopes=("search", "workspace-read"),
        workspace_id=workspace_id,
        workspace_role="owner",
        role="owner",
        token_kind="mcp_client",
    )
    ctx = ToolCallContext(
        actor=actor,
        db=DatabaseProvider(None, session_factory=session_factory),
        page_service=page_service,
        search_service=SearchService(retrievers=[remote]),
        services=ServiceRegistry(),
        workspace_id=workspace_id,
    )

    result = mcp_search(
        {
            "workspace_id": str(workspace_id),
            "query": "missing remote mcp evidence",
        },
        ctx,
    )

    assert result.data is not None
    assert result.data.warnings == ["coverage_gap_recorded"]
    assert [warning.code for warning in result.envelope.warnings] == [
        "coverage_gap_recorded"
    ]
    assert remote.calls == 1
    with session_factory() as session:
        assert session.scalar(select(func.count(KnowledgeCoverageGap.id))) == 1


def test_ask_warns_without_recording_gap(
    session_factory,
    monkeypatch,
):
    workspace_id = _workspace_id(session_factory)
    actor = _owner(workspace_id)
    service = AskService(search_service=SearchService())
    with session_factory.begin() as session:
        ask_session = service.create_session(
            session=session,
            workspace_id=workspace_id,
            request=AskSessionCreate(),
            actor_kind="human",
            actor_id=actor.actor_id,
            actor=actor,
        )
    _set_postgresql_dialect(monkeypatch, session_factory)

    with session_factory.begin() as session:
        detail, warnings = service.create_turn(
            session=session,
            workspace_id=workspace_id,
            ask_session_id=ask_session.id,
            request=AskTurnCreate(user_message="missing postgres evidence"),
            actor_kind="human",
            actor_id=actor.actor_id,
            actor=actor,
        )

    assert warnings == [KEYWORD_LANE_UNAVAILABLE_WARNING]
    assert detail.warning_codes == [KEYWORD_LANE_UNAVAILABLE_WARNING]
    with session_factory() as session:
        assert session.scalar(select(func.count(KnowledgeCoverageGap.id))) == 0

from uuid import uuid4

from sqlalchemy import func, select

from backend.app.container import DatabaseProvider, ServiceRegistry, ToolCallContext
from backend.app.settings import Settings
from backend.mcp import read_tools
from backend.models.coverage import KnowledgeCoverageGap
from backend.models.workspace import Workspace
from backend.policy.auth import ActorContext
from backend.schemas.read import RankedResult, SearchPayload


class FakeSearchService:
    def __init__(self, results):
        self.results = results
        self.calls = 0

    def search(self, **_kwargs):
        self.calls += 1
        return SearchPayload(
            ranked_results=[result.model_copy(deep=True) for result in self.results],
            supported_retrieval_modes=["keyword"],
            permission_note=None,
            next_cursor=None,
            tried_lanes=["entity", "keyword"],
            raw_query="needle",
            normalized_query="needle",
            total_count=len(self.results),
        )


class QueryRewriteSearchService(FakeSearchService):
    def __init__(self, results):
        super().__init__(results)
        self.queries = []

    def search(self, **kwargs):
        self.calls += 1
        query = kwargs["query"]
        self.queries.append(query)
        results = self.results if query == "rollback window" else []
        return SearchPayload(
            ranked_results=[result.model_copy(deep=True) for result in results],
            supported_retrieval_modes=["keyword"],
            permission_note=None,
            next_cursor=None,
            tried_lanes=["entity", "keyword"],
            raw_query=query,
            normalized_query=query,
            total_count=len(results),
        )


def _context(session_factory, search_service, workspace_id):
    return ToolCallContext(
        actor=ActorContext(
            actor_kind="human",
            actor_id="borrowed-rerank-test",
            scopes=("*",),
            workspace_id=workspace_id,
            workspace_role="editor",
            role="editor",
        ),
        db=DatabaseProvider(None, session_factory=session_factory),
        page_service=None,
        search_service=search_service,
        services=ServiceRegistry(),
        workspace_id=workspace_id,
    )


def _results(count=12, *, pinned=False):
    return [
        RankedResult(
            id=uuid4(),
            type="context_page",
            title=f"Result {index}",
            snippet=("x" * 200),
            score=1.0 - index / 100,
            citation_available=True,
            match_reason="exact_title" if pinned and index == 0 else "keyword",
        )
        for index in range(count)
    ]


def test_borrowed_rerank_preview_round_trip_reorders_signed_candidates(
    session_factory,
    monkeypatch,
):
    with session_factory() as session:
        workspace_id = session.scalar(
            select(Workspace.id).where(Workspace.slug == "default")
        )
    results = _results()
    # Versioned hits of one page need distinct signed identities, and a client
    # ranking must preserve publication priority inside the selected page.
    results[-2].version_id = uuid4()
    results[-2].version_status = "published_snapshot"
    results[-1].id = results[-2].id
    results[-1].version_id = uuid4()
    results[-1].version_status = "draft"
    results[-1].citation_available = False
    claim_page_id, claim_version_id = uuid4(), uuid4()
    for result in results[-4:-2]:
        result.type = "context_claim"
        result.version_id = claim_version_id
        result.source_ref = {"kind": "context_page", "id": str(claim_page_id)}
    # UUID uniqueness is table-scoped. Item kind must also participate in the
    # signed identity when an artifact and claim happen to share a UUID.
    results[-5].type = "library_artifact"
    results[-5].id = results[-4].id
    service = FakeSearchService(results)
    ctx = _context(session_factory, service, workspace_id)
    monkeypatch.setattr(
        read_tools,
        "get_settings",
        lambda: Settings(
            session_secret="rerank-secret",
            mcp_preview_2026_07_28=True,
        ),
    )

    first = read_tools.search(
        {
            "workspace_id": str(workspace_id),
            "query": "needle",
            "wire": "mrtr_preview",
        },
        ctx,
    )
    assert first.input_required is not None, first.model_dump()
    request = first.input_required["inputRequests"][0]
    assert request["id"] == "rerank"
    assert "UNTRUSTED DOCUMENT EXCERPT" in request["untrustedContextNotice"]
    assert "untrusted document text" in request["instruction"]
    assert len(request["candidates"]) == 12
    assert max(len(candidate["snippet"]) for candidate in request["candidates"]) == 160
    candidate_ids = [candidate["id"] for candidate in request["candidates"]]
    assert len(set(candidate_ids)) == len(results)
    ranking = [*reversed(candidate_ids), str(uuid4())]

    # A document may resume with a chunk representative. Its identity stays
    # stable, while distinct claims from one version retain their own IDs.
    page_id = results[-2].id
    results[-2].id = uuid4()
    results[-2].source_ref = {
        "kind": "content_chunk",
        "document_kind": "context_page",
        "document_id": str(page_id),
        "version_id": str(results[-2].version_id),
    }

    second = read_tools.search(
        {
            "workspace_id": str(workspace_id),
            "query": "needle",
            "wire": "mrtr_preview",
            "resume_token": first.input_required["requestState"],
            "input_responses": {"rerank": {"ranking": ranking}},
        },
        ctx,
    )

    actual_versions = [
        (result.id, result.version_id) for result in second.data.ranked_results
    ]
    assert (
        actual_versions
        == [
            (result.id, result.version_id)
            for result in [
                results[-2],
                results[-1],
                results[-4],
                results[-3],
                *reversed(results[:-4]),
            ]
        ][:8]
    )
    assert {result.id for result in results[-4:-2]} <= {
        result.id for result in second.data.ranked_results
    }
    assert "borrowed_rerank_applied" in second.data.warnings
    assert second.input_required is None
    assert second.data.total_count == 8
    assert second.data.next_cursor is None
    assert service.calls == 2


def test_borrowed_rerank_is_absent_for_stable_flag_off_and_pinned_results(
    session_factory,
    monkeypatch,
):
    with session_factory() as session:
        workspace_id = session.scalar(
            select(Workspace.id).where(Workspace.slug == "default")
        )
    service = FakeSearchService(_results())
    ctx = _context(session_factory, service, workspace_id)
    monkeypatch.setattr(
        read_tools,
        "get_settings",
        lambda: Settings(
            session_secret="rerank-secret",
            mcp_preview_2026_07_28=False,
        ),
    )
    flag_off = read_tools.search(
        {
            "workspace_id": str(workspace_id),
            "query": "needle",
            "wire": "mrtr_preview",
        },
        ctx,
    )
    stable = read_tools.search(
        {"workspace_id": str(workspace_id), "query": "needle"},
        ctx,
    )
    assert flag_off.input_required is None
    assert stable.input_required is None
    assert "mrtr_preview_unavailable" in flag_off.data.warnings

    monkeypatch.setattr(
        read_tools,
        "get_settings",
        lambda: Settings(
            session_secret="rerank-secret",
            mcp_preview_2026_07_28=True,
        ),
    )
    pinned_service = FakeSearchService(_results(pinned=True))
    pinned = read_tools.search(
        {
            "workspace_id": str(workspace_id),
            "query": "needle",
            "wire": "mrtr_preview",
        },
        _context(session_factory, pinned_service, workspace_id),
    )
    assert pinned.input_required is None


def test_borrowed_rerank_rejects_tampered_state_before_search(
    session_factory,
    monkeypatch,
):
    with session_factory() as session:
        workspace_id = session.scalar(
            select(Workspace.id).where(Workspace.slug == "default")
        )
    service = FakeSearchService(_results())
    ctx = _context(session_factory, service, workspace_id)
    monkeypatch.setattr(
        read_tools,
        "get_settings",
        lambda: Settings(
            session_secret="rerank-secret",
            mcp_preview_2026_07_28=True,
        ),
    )
    first = read_tools.search(
        {
            "workspace_id": str(workspace_id),
            "query": "needle",
            "wire": "mrtr_preview",
        },
        ctx,
    )
    token = first.input_required["requestState"]
    tampered = token[:-1] + ("A" if token[-1] != "A" else "B")

    rejected = read_tools.search(
        {
            "workspace_id": str(workspace_id),
            "query": "needle",
            "wire": "mrtr_preview",
            "resume_token": tampered,
            "input_responses": {"rerank": {"ranking": []}},
        },
        ctx,
    )

    assert rejected.envelope.error_code == "client_state_invalid"
    assert rejected.data is None
    assert service.calls == 1


def test_borrowed_rerank_state_is_bound_to_the_original_search_request(
    session_factory,
    monkeypatch,
):
    with session_factory() as session:
        workspace_id = session.scalar(
            select(Workspace.id).where(Workspace.slug == "default")
        )
    service = FakeSearchService(_results())
    ctx = _context(session_factory, service, workspace_id)
    monkeypatch.setattr(
        read_tools,
        "get_settings",
        lambda: Settings(
            session_secret="rerank-secret",
            mcp_preview_2026_07_28=True,
        ),
    )
    first = read_tools.search(
        {
            "workspace_id": str(workspace_id),
            "query": "needle",
            "wire": "mrtr_preview",
        },
        ctx,
    )

    rejected = read_tools.search(
        {
            "workspace_id": str(workspace_id),
            "query": "different query",
            "wire": "mrtr_preview",
            "resume_token": first.input_required["requestState"],
            "input_responses": {"rerank": {"ranking": []}},
        },
        ctx,
    )

    assert rejected.envelope.error_code == "client_state_invalid"
    assert service.calls == 1


def test_borrowed_rerank_resume_reports_preview_disabled_warning(
    session_factory,
    monkeypatch,
):
    with session_factory() as session:
        workspace_id = session.scalar(
            select(Workspace.id).where(Workspace.slug == "default")
        )
    service = FakeSearchService(_results())
    ctx = _context(session_factory, service, workspace_id)
    monkeypatch.setattr(
        read_tools,
        "get_settings",
        lambda: Settings(
            session_secret="rerank-secret",
            mcp_preview_2026_07_28=True,
        ),
    )
    first = read_tools.search(
        {
            "workspace_id": str(workspace_id),
            "query": "needle",
            "wire": "mrtr_preview",
        },
        ctx,
    )
    monkeypatch.setattr(
        read_tools,
        "get_settings",
        lambda: Settings(
            session_secret="rerank-secret",
            mcp_preview_2026_07_28=False,
        ),
    )

    rejected = read_tools.search(
        {
            "workspace_id": str(workspace_id),
            "query": "needle",
            "wire": "mrtr_preview",
            "resume_token": first.input_required["requestState"],
            "input_responses": {"rerank": {"ranking": []}},
        },
        ctx,
    )

    assert rejected.envelope.error_code == "client_state_invalid"
    assert [warning.code for warning in rejected.envelope.warnings] == [
        "mrtr_preview_unavailable"
    ]


def test_borrowed_rerank_resume_does_not_record_a_coverage_gap(
    session_factory,
    monkeypatch,
):
    with session_factory() as session:
        workspace_id = session.scalar(
            select(Workspace.id).where(Workspace.slug == "default")
        )
    results = _results()
    service = FakeSearchService(results)
    ctx = _context(session_factory, service, workspace_id)
    monkeypatch.setattr(
        read_tools,
        "get_settings",
        lambda: Settings(
            session_secret="rerank-secret",
            mcp_preview_2026_07_28=True,
        ),
    )
    first = read_tools.search(
        {
            "workspace_id": str(workspace_id),
            "query": "needle",
            "wire": "mrtr_preview",
        },
        ctx,
    )
    service.results = []

    second = read_tools.search(
        {
            "workspace_id": str(workspace_id),
            "query": "needle",
            "wire": "mrtr_preview",
            "resume_token": first.input_required["requestState"],
            "input_responses": {"rerank": {"ranking": []}},
        },
        ctx,
    )

    with session_factory() as session:
        gap_count = session.scalar(select(func.count(KnowledgeCoverageGap.id)))
    assert gap_count == 0
    assert second.data.ranked_results == []
    assert second.input_required is None


def test_borrowed_query_rewrite_round_trip_records_and_resolves_gap(
    session_factory,
    monkeypatch,
):
    with session_factory() as session:
        workspace_id = session.scalar(
            select(Workspace.id).where(Workspace.slug == "default")
        )
    service = QueryRewriteSearchService(_results(2))
    ctx = _context(session_factory, service, workspace_id)
    monkeypatch.setattr(
        read_tools,
        "get_settings",
        lambda: Settings(
            session_secret="rewrite-secret",
            mcp_preview_2026_07_28=True,
        ),
    )

    first = read_tools.search(
        {
            "workspace_id": str(workspace_id),
            "query": "When can we undo a deployment?",
            "wire": "mrtr_preview",
        },
        ctx,
    )
    assert first.input_required is not None, first.model_dump()
    request = first.input_required["inputRequests"][0]
    assert request["id"] == "rewrite"
    assert first.data.total_count == 0

    second = read_tools.search(
        {
            "workspace_id": str(workspace_id),
            "query": "When can we undo a deployment?",
            "wire": "mrtr_preview",
            "resume_token": first.input_required["requestState"],
            "input_responses": {"rewrite": {"query": "rollback window"}},
        },
        ctx,
    )

    assert service.queries == ["When can we undo a deployment?", "rollback window"]
    assert second.data.total_count == 2
    assert "borrowed_query_rewrite_applied" in second.data.warnings
    assert second.input_required is None
    with session_factory() as session:
        gap = session.scalar(select(KnowledgeCoverageGap))
        assert gap.status == "resolved"
        assert gap.rewrite_attempts == [
            {
                "query": "rollback window",
                "result_count": 2,
                "tried_lanes": ["entity", "keyword"],
            }
        ]

"""Sprint-4 C2: deterministic planner, direct resolver and trigram lane."""

from uuid import uuid4

import pytest
from sqlalchemy import select

from backend.llm.base import RerankResponse, RerankResult
from backend.models.workspace import Workspace
from backend.policy.auth import ActorContext
from backend.schemas.context_write import WriteDraftRequest
from backend.schemas.read import RankedResult, SearchFilters
from backend.scripts.seed_dev import seed_default_workspace
from backend.services.context_write_service import ContextWriteService
from backend.services.page_service import PageService
from backend.services.search_query import QueryPlan
from backend.services.search_service import (
    ADVISORY_SEARCH_FILTERS,
    APPLIED_SEARCH_FILTERS,
    SearchService,
)

pytestmark = pytest.mark.integration


class CountingRetriever:
    def __init__(self):
        self.calls = 0

    def supported_modes(self):
        return {"keyword"}

    def retrieve(self, **_kwargs):
        self.calls += 1
        return []


class StaticRetriever:
    def __init__(self, results):
        self.results = results

    def supported_modes(self):
        return {"keyword"}

    def retrieve(self, **_kwargs):
        return list(self.results)


class RecordingReranker:
    def __init__(self):
        self.documents: list[str] = []

    def rerank(self, request):
        self.documents = list(request.documents)
        return RerankResponse(
            provider="test",
            model="test",
            results=[
                RerankResult(index=index, score=float(len(request.documents) - index))
                for index in range(len(request.documents))
            ],
        )


def _workspace_id(session):
    return session.scalar(select(Workspace.id).where(Workspace.slug == "default"))


def _actor(role: str) -> ActorContext:
    return ActorContext(
        actor_kind="human",
        actor_id=f"planner-{role}",
        token_kind="session",
        scopes=(),
        workspace_role=role,
        role=role,
    )


def test_query_plan_preserves_raw_query_and_classifies_intent():
    entity = QueryPlan.build("  PAY-401  ")
    assert entity.raw_query == "  PAY-401  "
    assert entity.query_norm == "pay-401"
    assert entity.tokens == ("PAY-401",)
    assert entity.intent == "entity_lookup"

    assert QueryPlan.build("/context/deploy-runbook.md").intent == "direct_path"
    assert QueryPlan.build(str(uuid4())).intent == "direct_path"
    assert QueryPlan.build("결제 시스템 전체 구조").intent == "overview"
    assert QueryPlan.build("배포 롤백 정책").intent == "concept_lookup"
    assert QueryPlan.build("  ").intent == "fallback"

    assert APPLIED_SEARCH_FILTERS.isdisjoint(ADVISORY_SEARCH_FILTERS)
    assert APPLIED_SEARCH_FILTERS | ADVISORY_SEARCH_FILTERS == set(
        SearchFilters.model_fields
    )


def test_direct_path_slug_and_uuid_short_circuit_fallback(session_factory):
    page = PageService(session_factory).write_page(
        workspace_slug="default",
        slug="direct-runbook",
        title="Direct Runbook",
        body_md="# Direct Runbook\n\nDirect body.",
        actor_kind="human",
        actor_id="planner-test",
        actor_provenance="browser_session",
    )
    retriever = CountingRetriever()
    service = SearchService(retrievers=[retriever])

    with session_factory() as session:
        workspace_id = _workspace_id(session)
        by_path = service.search(
            session=session,
            workspace_id=workspace_id,
            query="/context/direct-runbook.md",
            scope="context_page",
        )
        by_slug = service.search(
            session=session,
            workspace_id=workspace_id,
            query="direct-runbook",
            scope="context_page",
        )
        by_id = service.search(
            session=session,
            workspace_id=workspace_id,
            query=str(page.id),
            scope="context_page",
        )

    assert retriever.calls == 0
    assert [result.id for result in by_path.ranked_results] == [page.id]
    assert [result.id for result in by_slug.ranked_results] == [page.id]
    assert [result.id for result in by_id.ranked_results] == [page.id]
    assert by_path.ranked_results[0].match_reason == "exact_slug"
    assert by_slug.ranked_results[0].match_reason == "exact_slug"
    assert by_id.ranked_results[0].match_reason == "exact_id"
    assert by_path.tried_lanes == by_slug.tried_lanes == by_id.tried_lanes == [
        "direct"
    ]


def test_bare_slug_collision_disambiguates_but_explicit_path_stays_direct(
    session_factory,
):
    slug_page = PageService(session_factory).write_page(
        workspace_slug="default",
        slug="pipeline",
        title="Pipeline Runtime",
        body_md="# Pipeline Runtime\n\nSlug target.",
        actor_kind="human",
        actor_id="planner-test",
        actor_provenance="browser_session",
    )
    title_page = PageService(session_factory).write_page(
        workspace_slug="default",
        slug="pipeline-overview",
        title="Pipeline",
        body_md="# Pipeline\n\nTitle target.",
        actor_kind="human",
        actor_id="planner-test",
        actor_provenance="browser_session",
    )
    retriever = CountingRetriever()
    service = SearchService(retrievers=[retriever])

    with session_factory() as session:
        workspace_id = _workspace_id(session)
        ambiguous = service.search(
            session=session,
            workspace_id=workspace_id,
            query="pipeline",
            scope="context_page",
        )
        quoted = service.search(
            session=session,
            workspace_id=workspace_id,
            query='"Pipeline"',
            scope="context_page",
        )
        explicit = service.search(
            session=session,
            workspace_id=workspace_id,
            query="/context/pipeline.md",
            scope="context_page",
        )

    expected_ids = {slug_page.id, title_page.id}
    assert {result.id for result in ambiguous.ranked_results} == expected_ids
    assert {result.id for result in quoted.ranked_results} == expected_ids
    assert "disambiguation_required" in ambiguous.warnings
    assert "disambiguation_required" in quoted.warnings
    assert len(ambiguous.disambiguation.candidates) == 2
    assert len(quoted.disambiguation.candidates) == 2
    assert [result.id for result in explicit.ranked_results] == [slug_page.id]
    assert explicit.ranked_results[0].match_reason == "exact_slug"
    assert explicit.tried_lanes == ["direct"]
    assert retriever.calls == 0


def test_direct_uuid_rechecks_workspace_and_draft_visibility(session_factory):
    with session_factory.begin() as session:
        default_workspace_id = _workspace_id(session)
        other = seed_default_workspace(
            session,
            slug="planner-other",
            title="Planner Other",
        )
        draft = ContextWriteService().write_draft(
            session=session,
            workspace_id=default_workspace_id,
            request=WriteDraftRequest(
                title="Direct Private Draft",
                page_type="concept",
                body_markdown="# Direct Private Draft\n\nDraft body.",
                reason="direct visibility test",
            ),
            actor_kind="human",
            actor_id="planner-test",
            actor_role="owner",
        )
    other_page = PageService(session_factory).write_page(
        workspace_slug=other.slug,
        slug="other-workspace-page",
        title="Other Workspace Page",
        body_md="# Other Workspace Page\n\nOther body.",
        actor_kind="human",
        actor_id="planner-test",
        actor_provenance="browser_session",
    )
    service = SearchService(retrievers=[CountingRetriever()])

    with session_factory() as session:
        viewer_draft = service.search(
            session=session,
            workspace_id=default_workspace_id,
            query=str(draft.page_id),
            scope="context_page",
            actor=_actor("viewer"),
        )
        editor_draft = service.search(
            session=session,
            workspace_id=default_workspace_id,
            query=str(draft.page_id),
            scope="context_page",
            actor=_actor("editor"),
        )
        cross_workspace = service.search(
            session=session,
            workspace_id=default_workspace_id,
            query=str(other_page.id),
            scope="context_page",
            actor=_actor("editor"),
        )

    assert viewer_draft.ranked_results == []
    assert [result.id for result in editor_draft.ranked_results] == [draft.page_id]
    assert cross_workspace.ranked_results == []


def test_trigram_finds_typo_and_partial_but_two_chars_stay_exact_only(
    session_factory,
):
    page_service = PageService(session_factory)
    target = page_service.write_page(
        workspace_slug="default",
        slug="deployment-checklist",
        title="Deployment Checklist",
        body_md="# Deployment Checklist\n\nRoutine steps.",
        actor_kind="human",
        actor_id="planner-test",
        actor_provenance="browser_session",
    )
    short = page_service.write_page(
        workspace_slug="default",
        slug="database-reference",
        title="DB",
        body_md="# DB\n\nDatabase reference.",
        actor_kind="human",
        actor_id="planner-test",
        actor_provenance="browser_session",
    )
    retriever = CountingRetriever()
    service = SearchService(retrievers=[retriever])

    with session_factory() as session:
        workspace_id = _workspace_id(session)
        typo = service.search(
            session=session,
            workspace_id=workspace_id,
            query="Deploymnt Checklis",
            scope="context_page",
        )
        partial = service.search(
            session=session,
            workspace_id=workspace_id,
            query="ployment check",
            scope="context_page",
        )
        exact_short = service.search(
            session=session,
            workspace_id=workspace_id,
            query="DB",
            scope="context_page",
        )
        short_miss = service.search(
            session=session,
            workspace_id=workspace_id,
            query="DP",
            scope="context_page",
        )
        long_name_miss = service.search(
            session=session,
            workspace_id=workspace_id,
            query="x" * 161,
            scope="context_page",
        )

    assert typo.ranked_results[0].id == target.id
    assert partial.ranked_results[0].id == target.id
    assert typo.ranked_results[0].match_reason == "trigram"
    assert partial.ranked_results[0].match_reason == "trigram"
    assert typo.tried_lanes == partial.tried_lanes == [
        "entity",
        "trigram",
        "keyword",
    ]
    assert exact_short.ranked_results[0].id == short.id
    assert exact_short.ranked_results[0].match_reason == "exact_title"
    assert exact_short.tried_lanes == ["entity"]
    assert short_miss.ranked_results == []
    assert short_miss.tried_lanes == ["entity", "keyword"]
    assert long_name_miss.tried_lanes == ["entity", "keyword"]


def test_trigram_rechecks_filters_and_draft_visibility(session_factory):
    page_service = PageService(session_factory)
    concept = page_service.write_page(
        workspace_slug="default",
        slug="incident-playbook",
        title="Incident Playbook",
        body_md="# Incident Playbook\n\nConcept body.",
        page_type="concept",
        actor_kind="human",
        actor_id="planner-test",
        actor_provenance="browser_session",
    )
    page_service.write_page(
        workspace_slug="default",
        slug="incident-decision",
        title="Incident Decision",
        body_md="# Incident Decision\n\nDecision body.",
        page_type="decision",
        actor_kind="human",
        actor_id="planner-test",
        actor_provenance="browser_session",
    )
    retriever = CountingRetriever()
    service = SearchService(retrievers=[retriever])

    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        draft = ContextWriteService().write_draft(
            session=session,
            workspace_id=workspace_id,
            request=WriteDraftRequest(
                title="Private Incident Guide",
                page_type="concept",
                body_markdown="# Private Incident Guide\n\nDraft only.",
                reason="planner visibility test",
            ),
            actor_kind="human",
            actor_id="planner-test",
            actor_role="owner",
        )

    with session_factory() as session:
        workspace_id = _workspace_id(session)
        filtered = service.search(
            session=session,
            workspace_id=workspace_id,
            query="Incidnt Playboo",
            scope="context_page",
            filters=SearchFilters(context_type=["concept"]),
            actor=_actor("viewer"),
        )
        viewer = service.search(
            session=session,
            workspace_id=workspace_id,
            query="Privte Incident Guid",
            scope="context_page",
            actor=_actor("viewer"),
        )
        editor = service.search(
            session=session,
            workspace_id=workspace_id,
            query="Privte Incident Guid",
            scope="context_page",
            actor=_actor("editor"),
        )

    assert filtered.ranked_results[0].id == concept.id
    assert all(result.concept_type == "concept" for result in filtered.ranked_results)
    assert draft.page_id not in {result.id for result in viewer.ranked_results}
    assert draft.page_id in {result.id for result in editor.ranked_results}


def test_reranker_receives_fallback_candidates_but_not_trigram_pin(
    session_factory,
):
    target = PageService(session_factory).write_page(
        workspace_slug="default",
        slug="rerank-deployment-checklist",
        title="Rerank Deployment Checklist",
        body_md="# Rerank Deployment Checklist\n\nRoutine steps.",
        actor_kind="human",
        actor_id="planner-test",
        actor_provenance="browser_session",
    )
    fallback = [
        RankedResult(
            id=uuid4(),
            type="context_page",
            title=f"Fallback {index}",
            snippet="fallback body",
            score=float(2 - index),
            citation_available=True,
        )
        for index in range(2)
    ]
    reranker = RecordingReranker()
    service = SearchService(
        retrievers=[StaticRetriever(fallback)],
        reranker=reranker,
    )

    with session_factory() as session:
        payload = service.search(
            session=session,
            workspace_id=_workspace_id(session),
            query="Rerank Deploymnt Checklis",
            scope="context_page",
        )

    assert payload.ranked_results[0].id == target.id
    assert reranker.documents == [
        "Fallback 0\nfallback body",
        "Fallback 1\nfallback body",
    ]

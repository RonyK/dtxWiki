import json
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import select
from typer.testing import CliRunner

from backend.app.db import make_engine, make_session_factory
from backend.app.settings import Settings
from backend.cli.commands.eval_cmd import _golden_page_samples
from backend.cli.main import app
from backend.models.base import Base
from backend.models.context import ContextCitation
from backend.models.delegated_task import ClientDelegatedTask
from backend.models.workspace import Workspace
from backend.schemas.context_write import (
    PublishContextPageRequest,
    ReviewDecisionRequest,
    WriteDraftRequest,
)
from backend.schemas.read import RankedResult, SearchPayload
from backend.scripts.seed_dev import seed_default_workspace
from backend.services.context_write_service import ContextWriteService
from backend.services.eval.harness import (
    GoldenQuery,
    evaluate,
    evaluate_lanes,
    load_golden,
    result_identifier,
)
from backend.services.eval.metrics import (
    hit_rate_at_k,
    recall_at_k,
    reciprocal_rank,
)
from backend.services.page_service import PageService
from backend.services.review_service import ReviewService
from backend.services.search_service import build_search_service

pytestmark = pytest.mark.integration

ACTOR_ID = "00000000-0000-0000-0000-000000000010"


def _workspace_id(session):
    return session.scalar(select(Workspace).where(Workspace.slug == "default")).id


def test_golden_page_samples_rank_inbound_citations(page_service):
    cited = page_service.write_page(
        workspace_slug="default",
        slug="z-cited-page",
        title="Z Cited Page",
        body_md="# Z Cited Page",
        actor_kind="human",
        actor_id=ACTOR_ID,
        actor_provenance="browser_session",
    )
    citing = page_service.write_page(
        workspace_slug="default",
        slug="a-citing-page",
        title="A Citing Page",
        body_md="# A Citing Page",
        actor_kind="human",
        actor_id=ACTOR_ID,
        actor_provenance="browser_session",
    )
    with page_service.session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        session.add(
            ContextCitation(
                workspace_id=workspace_id,
                target_kind="context_page",
                target_id=citing.id,
                target_location={},
                cited_context_page_version_id=cited.version_id,
                location={},
                citation_type="context_page",
            )
        )
        session.flush()

        samples = _golden_page_samples(session, workspace_id, limit=2)

    assert samples == [
        {
            "page_id": str(cited.id),
            "title": "Z Cited Page",
            "concept_path": "/context/z-cited-page.md",
            "citation_count": 1,
        },
        {
            "page_id": str(citing.id),
            "title": "A Citing Page",
            "concept_path": "/context/a-citing-page.md",
            "citation_count": 0,
        },
    ]


# --- pure metric math -------------------------------------------------------


def test_metric_functions():
    predicted = ["a", "b", "c", "d"]
    assert recall_at_k(predicted, ["a", "c"], 4) == 1.0
    assert recall_at_k(predicted, ["a", "x"], 4) == 0.5
    assert recall_at_k(predicted, ["a"], 1) == 1.0
    assert recall_at_k(predicted, ["b"], 1) == 0.0  # b is outside top-1
    assert recall_at_k(predicted, [], 4) == 0.0
    assert reciprocal_rank(predicted, ["b"]) == 0.5
    assert reciprocal_rank(predicted, ["x"]) == 0.0
    assert hit_rate_at_k(predicted, ["d"], 4) == 1.0
    assert hit_rate_at_k(predicted, ["d"], 3) == 0.0


def test_load_golden_parses_and_rejects_bad_shapes():
    golden = load_golden(
        [
            {"query": "q1", "expected": ["/a"], "lane": "entity"},
            {"query": "q2"},
        ]
    )
    assert golden[0] == GoldenQuery(query="q1", expected=["/a"], lane="entity")
    assert golden[1] == GoldenQuery(query="q2", expected=[])
    with pytest.raises(ValueError):
        load_golden({"not": "a list"})
    with pytest.raises(ValueError):
        load_golden([{"expected": ["/a"]}])  # missing query
    with pytest.raises(ValueError, match="lane must be one of"):
        load_golden([{"query": "q", "lane": "semantic"}])


# --- harness aggregation (stub service, no DB) ------------------------------


def _ranked(
    identifier: str,
    score: float,
    *,
    match_reason: str | None = None,
    result_type: str = "context_page",
) -> RankedResult:
    return RankedResult(
        id=uuid4(),
        type=result_type,
        title=identifier,
        snippet="",
        score=score,
        citation_available=False,
        concept_path=identifier,
        match_reason=match_reason,
    )


class _StubSearchService:
    def __init__(self, by_query: dict[str, list[str]]) -> None:
        self._by_query = by_query

    def search(self, *, query, limit, **_ignored) -> SearchPayload:
        identifiers = self._by_query.get(query, [])[:limit]
        results = [
            _ranked(identifier, 1.0 - index * 0.01)
            for index, identifier in enumerate(identifiers)
        ]
        return SearchPayload(
            ranked_results=results,
            supported_retrieval_modes=["keyword"],
            permission_note=None,
            next_cursor=None,
        )


def test_evaluate_aggregates_metrics_across_queries():
    golden = [
        GoldenQuery(query="q1", expected=["doc-a"]),
        GoldenQuery(query="q2", expected=["doc-b", "doc-c"]),
    ]
    stub = _StubSearchService({"q1": ["doc-a", "doc-x"], "q2": ["doc-z", "doc-b"]})
    [metrics] = evaluate(stub, None, None, golden, modes=["keyword"], k=8)
    assert metrics.mode == "keyword"
    assert metrics.queries == 2
    # q1 recall 1.0 (doc-a hit), q2 recall 0.5 (doc-b of {doc-b,doc-c}) → 0.75
    assert metrics.recall_at_k == pytest.approx(0.75)
    # q1 rr 1/1, q2 rr 1/2 → 0.75
    assert metrics.mrr == pytest.approx(0.75)
    assert metrics.hit_rate == pytest.approx(1.0)


def test_evaluate_lanes_separates_match_reason_families():
    class LaneStub:
        def search(self, *, query, **_ignored):
            expected_reason = {
                "entity query": "exact_title",
                "trigram query": "trigram",
                "bm25 query": "keyword",
            }[query]
            return SearchPayload(
                ranked_results=[
                    _ranked(
                        "/workflow-noise",
                        2.0,
                        match_reason="keyword",
                        result_type="feedback",
                    ),
                    _ranked(
                        f"/{query.split()[0]}",
                        1.0,
                        match_reason=expected_reason,
                    ),
                    _ranked("/wrong-lane", 0.5, match_reason="semantic"),
                ],
                supported_retrieval_modes=["keyword"],
                permission_note=None,
                next_cursor=None,
            )

    golden = [
        GoldenQuery("entity query", ["/entity"], lane="entity"),
        GoldenQuery("trigram query", ["/trigram"], lane="trigram"),
        GoldenQuery("bm25 query", ["/bm25"], lane="bm25"),
    ]
    metrics = evaluate_lanes(LaneStub(), None, None, golden, k=3)

    assert [metric.lane for metric in metrics] == ["entity", "trigram", "bm25"]
    assert all(metric.queries == 1 for metric in metrics)
    assert all(metric.recall_at_k == 1.0 for metric in metrics)
    assert all(metric.mrr == 1.0 for metric in metrics)
    assert all(metric.hit_rate == 1.0 for metric in metrics)


def test_ranked_result_rejects_unknown_match_reason():
    with pytest.raises(ValidationError):
        _ranked("/bad", 1.0, match_reason="exact-titel")


# --- real keyword retrieval end-to-end --------------------------------------


def test_evaluate_keyword_mode_over_published_page(session_factory):
    service = build_search_service(Settings())
    assert "keyword" in service.supported_modes()

    write = ContextWriteService()
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        created = write.write_draft(
            session=session,
            workspace_id=workspace_id,
            request=WriteDraftRequest(
                title="Vector Search Tuning",
                page_type="overview",
                body_markdown=(
                    "# Vector Search Tuning\n\n"
                    "How to tune the vector index for retrieval."
                ),
                reason="seed",
            ),
            actor_kind="human",
            actor_id=ACTOR_ID,
            actor_role="owner",
        )
        ReviewService().apply_decision(
            session=session,
            workspace_id=workspace_id,
            review_item_id=created.review_item_id,
            request=ReviewDecisionRequest(decision_type="approve"),
            actor_kind="human",
            actor_id=ACTOR_ID,
            actor_role="owner",
            actor_provenance="browser_session",
        )
        write.publish_draft(
            session=session,
            workspace_id=workspace_id,
            page_id=created.page_id,
            request=PublishContextPageRequest(
                version_id=created.draft_version_id,
                reason="publish",
            ),
            actor_kind="human",
            actor_id=ACTOR_ID,
        )

    # Discover the identifier the keyword retriever assigns to the page, so the
    # golden set is independent of the concept_path format.
    with session_factory() as session:
        payload = service.search(
            session=session,
            workspace_id=workspace_id,
            query="vector",
            retrieval_mode="keyword",
            limit=8,
        )
        assert payload.ranked_results
        identifier = result_identifier(payload.ranked_results[0])

    with session_factory() as session:
        [metrics] = evaluate(
            service,
            session,
            workspace_id,
            [GoldenQuery(query="vector", expected=[identifier])],
            modes=["keyword"],
            k=8,
        )
    assert metrics.recall_at_k == 1.0
    assert metrics.hit_rate == 1.0
    assert metrics.mrr > 0.0

    # Negative control: an expected document that is never retrieved scores 0.
    with session_factory() as session:
        [miss] = evaluate(
            service,
            session,
            workspace_id,
            [GoldenQuery(query="vector", expected=["/context/does-not-exist"])],
            modes=["keyword"],
            k=8,
        )
    assert miss.recall_at_k == 0.0
    assert miss.hit_rate == 0.0


def test_eval_cli_prints_mode_and_lane_tables(tmp_path):
    database_url = f"sqlite+pysqlite:///{tmp_path / 'eval.sqlite'}"
    engine = make_engine(database_url)
    Base.metadata.create_all(engine)
    session_factory = make_session_factory(engine)
    with session_factory.begin() as session:
        seed_default_workspace(session)
    PageService(session_factory).write_page(
        workspace_slug="default",
        slug="cli-lane-guide",
        title="CLI Lane Guide",
        body_md="# CLI Lane Guide\n\nCLI lane body.",
        actor_kind="human",
        actor_id=ACTOR_ID,
        actor_provenance="browser_session",
    )
    golden_path = tmp_path / "golden.json"
    golden_path.write_text(
        json.dumps(
            [
                {
                    "query": "CLI Lane Guide",
                    "expected": ["/context/cli-lane-guide.md"],
                    "lane": "entity",
                }
            ]
        ),
        encoding="utf-8",
    )

    result = CliRunner().invoke(
        app,
        [
            "eval",
            "--golden",
            str(golden_path),
            "--workspace",
            "default",
            "--db",
            database_url,
        ],
    )

    assert result.exit_code == 0, result.output
    assert "mode" in result.output and "keyword" in result.output
    assert "lane" in result.output and "entity" in result.output

    golden_before = golden_path.read_bytes()
    proposed = CliRunner().invoke(
        app,
        [
            "eval",
            "--golden",
            str(golden_path),
            "--workspace",
            "default",
            "--db",
            database_url,
            "--propose",
        ],
    )
    assert proposed.exit_code == 0, proposed.output
    assert "golden query proposal: queued" in proposed.output
    with session_factory() as session:
        task = session.scalar(
            select(ClientDelegatedTask).where(
                ClientDelegatedTask.task_type == "golden_query_candidates"
            )
        )
        assert task is not None
        assert task.payload["page_samples"][0]["concept_path"] == (
            "/context/cli-lane-guide.md"
        )
    assert golden_path.read_bytes() == golden_before

    failing_baseline = tmp_path / "baseline.json"
    failing_baseline.write_text(
        json.dumps(
            {
                "threshold": 0.05,
                "modes": {"keyword": {"recall_at_k": 1.06}},
            }
        ),
        encoding="utf-8",
    )
    failed = CliRunner().invoke(
        app,
        [
            "eval",
            "--golden",
            str(golden_path),
            "--workspace",
            "default",
            "--db",
            database_url,
            "--baseline",
            str(failing_baseline),
        ],
    )
    assert failed.exit_code == 1
    assert "eval regression gate: FAILED" in failed.output

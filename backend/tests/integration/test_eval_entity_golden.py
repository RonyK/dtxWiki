"""Entity, trigram and BM25 golden gates (Epic-1 design §6.2/§13).

CI-pinned quality bars for exact-name retrieval:

- exact title/slug/alias queries resolve the target page at rank 1 (100%),
- exact name collisions retain every matching page in the ranked list,
- a renamed page is exact-hit by title under its new name and alias under its old one.

Sprint-4 N3 adds explicit per-lane metrics over the FTS5 projections.
"""

import pytest
from sqlalchemy import select

from backend.app.settings import Settings
from backend.models.context import ContextPage, ContextPageVersion, PageNameIndex
from backend.models.workspace import Workspace
from backend.services.eval.harness import (
    GoldenQuery,
    evaluate,
    evaluate_lanes,
    result_identifier,
)
from backend.services.page_service import sync_page_projections
from backend.services.search_service import build_search_service

pytestmark = pytest.mark.integration


def _seed_published_page(
    session,
    workspace_id,
    *,
    slug,
    title,
    aliases=(),
    body=None,
):
    page = ContextPage(
        workspace_id=workspace_id,
        slug=slug,
        title=title,
        page_type="concept",
        aliases=list(aliases),
        lifecycle_status="published",
    )
    session.add(page)
    session.flush()
    version = ContextPageVersion(
        workspace_id=workspace_id,
        page_id=page.id,
        version_no=1,
        body_md=body or f"# {title}\n\nBody.",
        content_hash=f"{slug}-v1",
        actor_type="human",
        created_by_actor_kind="human",
        created_by_actor_id="tester",
        version_status="published_snapshot",
    )
    session.add(version)
    session.flush()
    page.current_published_version_id = version.id
    page.current_version_id = version.id
    sync_page_projections(session, page=page, version=version)
    return page


def _workspace_id(session):
    return session.scalar(select(Workspace.id).where(Workspace.slug == "default"))


def test_entity_exact_golden_top1_is_100_percent(session_factory):
    service = build_search_service(Settings())
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        retriever = _seed_published_page(
            session,
            workspace_id,
            slug="retriever-protocol",
            title="Retriever Protocol",
            aliases=["검색 프로토콜"],
        )
        planner = _seed_published_page(
            session,
            workspace_id,
            slug="query-planner",
            title="Query Planner",
        )

    # Identifier discovery: ask the service once per page, then pin top-1
    # via MRR over the golden set (mrr == 1.0 ⇔ every query hits rank 1).
    with session_factory() as session:
        by_page = {}
        for query, page in [
            ("Retriever Protocol", retriever),
            ("Query Planner", planner),
        ]:
            payload = service.search(
                session=session,
                workspace_id=workspace_id,
                query=query,
                retrieval_mode="keyword",
                limit=8,
            )
            assert payload.ranked_results, f"no results for {query!r}"
            top = payload.ranked_results[0]
            assert top.id == page.id
            by_page[page.id] = result_identifier(top)

    golden = [
        # exact title
        GoldenQuery(query="Retriever Protocol", expected=[by_page[retriever.id]]),
        GoldenQuery(query="Query Planner", expected=[by_page[planner.id]]),
        # exact slug
        GoldenQuery(query="retriever-protocol", expected=[by_page[retriever.id]]),
        GoldenQuery(query="query-planner", expected=[by_page[planner.id]]),
        # exact alias (Korean) + case/spacing normalization
        GoldenQuery(query="검색 프로토콜", expected=[by_page[retriever.id]]),
        GoldenQuery(query="  retriever   protocol ", expected=[by_page[retriever.id]]),
    ]
    with session_factory() as session:
        [metrics] = evaluate(
            service, session, workspace_id, golden, modes=["keyword"], k=8
        )
    assert metrics.recall_at_k == 1.0
    assert metrics.hit_rate == 1.0
    assert metrics.mrr == 1.0  # top-1 for every exact query


def test_entity_name_collision_golden_returns_all_ranked_matches(session_factory):
    service = build_search_service(Settings())
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        _seed_published_page(
            session,
            workspace_id,
            slug="pipeline-batch",
            title="Pipeline",
        )
        stream = _seed_published_page(
            session,
            workspace_id,
            slug="pipeline-stream",
            title="Streaming Pipeline",
        )
        # Simulate a pre-B-4 collision already present in the rebuildable
        # projection; authored aliases now reject creating this state.
        session.add(
            PageNameIndex(
                workspace_id=workspace_id,
                page_id=stream.id,
                source_version_id=stream.current_published_version_id,
                visibility_scope="published",
                name_norm="pipeline",
                name="Pipeline",
                name_kind="alias",
            )
        )

    ambiguous_queries = ["Pipeline", "pipeline"]
    with session_factory() as session:
        for query in ambiguous_queries:
            payload = service.search(
                session=session,
                workspace_id=workspace_id,
                query=query,
                retrieval_mode="keyword",
                limit=8,
            )
            assert len(payload.ranked_results) == payload.total_count == 2, query
            assert [hit.match_reason for hit in payload.ranked_results] == [
                "exact_title", "exact_alias"
            ], query


def test_entity_rename_golden_retires_old_title_immediately(session_factory):
    """P2.3: renaming no longer auto-preserves the retired title as an alias.

    The old title/slug is reclaimed immediately, not kept as a searchable
    alias -- only an explicitly requested `aliases` entry survives a rename.
    """

    service = build_search_service(Settings())
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        page = _seed_published_page(
            session,
            workspace_id,
            slug="release-notes",
            title="Release Notes",
        )
        # Rename through the projection chokepoint (mirrors tree/REST rename).
        page.title = "Changelog"
        sync_page_projections(session, page=page)

    with session_factory() as session:
        renamed = service.search(
            session=session,
            workspace_id=workspace_id,
            query="Changelog",
            retrieval_mode="keyword",
            limit=8,
        )
        assert renamed.ranked_results
        top = renamed.ranked_results[0]
        assert top.id == page.id
        assert top.match_reason == "exact_title"

        stale = service.search(
            session=session,
            workspace_id=workspace_id,
            query="Release Notes",
            retrieval_mode="keyword",
            limit=8,
        )
        # The seeded body is "# Release Notes\n\nBody." (mirrors the title at
        # creation time, never updated by a rename), so a generic full-text
        # match is still legitimate -- what must be gone is the STRONG
        # exact_title/exact_alias match a preserved alias used to produce.
        stale_match = next(
            (result for result in stale.ranked_results if result.id == page.id),
            None,
        )
        assert stale_match is None or stale_match.match_reason not in (
            "exact_title",
            "exact_alias",
        )


def test_per_lane_golden_baselines_are_green(session_factory):
    service = build_search_service(Settings())
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        exact = _seed_published_page(
            session,
            workspace_id,
            slug="lane-exact-guide",
            title="Lane Exact Guide",
        )
        trigram = _seed_published_page(
            session,
            workspace_id,
            slug="lane-deployment-checklist",
            title="Lane Deployment Checklist",
        )
        _seed_published_page(
            session,
            workspace_id,
            slug="lane-deployment-checklist-archive",
            title="Lane Deployment Checklist Archive",
        )
        bm25 = _seed_published_page(
            session,
            workspace_id,
            slug="lane-body-source",
            title="Lane Body Source",
            body=(
                "# Lane Body Source\n\n"
                "Photon calibration evidence appears only in this body."
            ),
        )

    golden = [
        GoldenQuery(
            "Lane Exact Guide",
            [f"/context/{exact.slug}.md"],
            lane="entity",
        ),
        GoldenQuery(
            "Lane Deploymnt Checklis",
            [f"/context/{trigram.slug}.md"],
            lane="trigram",
        ),
        GoldenQuery(
            "photon calibration",
            [f"/context/{bm25.slug}.md"],
            lane="bm25",
        ),
    ]
    with session_factory() as session:
        metrics = evaluate_lanes(
            service,
            session,
            workspace_id,
            golden,
            k=3,
        )

    assert [metric.lane for metric in metrics] == ["entity", "trigram", "bm25"]
    assert all(metric.recall_at_k == 1.0 for metric in metrics)
    assert all(metric.hit_rate == 1.0 for metric in metrics)
    assert all(metric.mrr == 1.0 for metric in metrics)

    with session_factory() as session:
        below_threshold = service.search(
            session=session,
            workspace_id=workspace_id,
            query="deployment catastrophe",
            retrieval_mode="keyword",
            limit=3,
        )
    assert all(
        result.match_reason != "trigram"
        for result in below_threshold.ranked_results
    )

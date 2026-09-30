from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from backend.models.base import Base
from backend.models.coverage import KnowledgeCoverageGap
from backend.models.delegated_task import ClientDelegatedTask
from backend.models.workspace import Workspace
from backend.scripts.seed_dev import seed_default_workspace
from backend.services.coverage_service import (
    coverage_candidate_visible,
    list_coverage_gaps,
    record_coverage_gap,
    resolve_coverage_gap,
)


def test_list_coverage_gaps_filters_restricted_candidates_by_actor():
    # Codex round-5: GET /coverage-gaps returned nearest_candidates verbatim, so
    # a viewer received candidates that were filtered for the gap's author. Drop
    # candidates the reader cannot see.

    from backend.app.db import make_engine, make_session_factory
    from backend.models.base import Base
    from backend.models.library import LibraryItem
    from backend.policy.auth import ActorContext

    engine = make_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = make_session_factory(engine)
    with factory.begin() as session:
        workspace = seed_default_workspace(session)
        wsid = workspace.id
        item = LibraryItem(
            workspace_id=wsid,
            connector_type="upload",
            format_hint="pdf",
            uri="https://example.com/restricted-cov.pdf",
            title="Restricted Coverage Source",
            trust_level="restricted",
            lifecycle_status="registered",
        )
        session.add(item)
        session.flush()
        session.add(
            KnowledgeCoverageGap(
                workspace_id=wsid,
                query_norm="restricted coverage",
                surface="ask",
                tried_lanes=["keyword"],
                nearest_candidates=[
                    {
                        "id": str(item.id),
                        "kind": "library_item",
                        "title": "Restricted Coverage Source",
                        "concept_path": "",
                        "match_reason": "trigram",
                    }
                ],
                status="open",
            )
        )

    viewer = ActorContext(
        actor_kind="human",
        actor_id="viewer",
        token_kind="session",
        scopes=("read",),
        workspace_id=wsid,
        workspace_role="viewer",
        role="viewer",
    )
    owner = ActorContext(
        actor_kind="system",
        actor_id="owner",
        scopes=("*",),
        workspace_id=wsid,
        workspace_role="owner",
        role="owner",
    )
    with factory() as session:
        viewer_gaps = list_coverage_gaps(session, workspace_id=wsid, actor=viewer)
        owner_gaps = list_coverage_gaps(session, workspace_id=wsid, actor=owner)

    assert viewer_gaps.items[0].nearest_candidates == []
    assert len(owner_gaps.items[0].nearest_candidates) == 1


def test_list_coverage_gaps_hides_foreign_candidate_without_mutating_gap(
    session_factory,
):
    from backend.models.library import LibraryItem

    with session_factory.begin() as session:
        workspace_id = session.scalar(
            select(Workspace.id).where(Workspace.slug == "default")
        )
        foreign_workspace = Workspace(
            slug="coverage-candidate-foreign",
            title="Coverage candidate foreign",
            name="Coverage candidate foreign",
        )
        session.add(foreign_workspace)
        session.flush()
        foreign_item = LibraryItem(
            workspace_id=foreign_workspace.id,
            connector_type="upload",
            format_hint="pdf",
            uri="https://example.com/foreign-coverage.pdf",
            title="Foreign Coverage Source",
            trust_level="trusted",
            lifecycle_status="registered",
        )
        session.add(foreign_item)
        session.flush()
        stored_candidate = {
            "id": str(foreign_item.id),
            "kind": "library_item",
            "title": "Foreign Coverage Source",
        }
        gap = KnowledgeCoverageGap(
            workspace_id=workspace_id,
            query_norm="foreign coverage candidate",
            surface="ask",
            tried_lanes=["keyword"],
            nearest_candidates=[stored_candidate],
            status="open",
        )
        session.add(gap)
        session.flush()

        result = list_coverage_gaps(session, workspace_id=workspace_id)

        assert result.items[0].nearest_candidates == []
        assert gap.nearest_candidates == [stored_candidate]


def test_list_coverage_gaps_drops_archived_and_tolerates_scalar():
    # Codex round-6: an archived/hidden candidate must be dropped even for an
    # owner (lifecycle check), and a corrupted scalar nearest_candidates must
    # fail closed (empty), not 500.
    from backend.app.db import make_engine, make_session_factory
    from backend.models.base import Base
    from backend.models.library import LibraryItem
    from backend.policy.auth import ActorContext

    engine = make_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = make_session_factory(engine)
    with factory.begin() as session:
        workspace = seed_default_workspace(session)
        wsid = workspace.id
        archived = LibraryItem(
            workspace_id=wsid,
            connector_type="upload",
            format_hint="pdf",
            uri="https://example.com/archived-cov.pdf",
            title="Archived Coverage Source",
            trust_level="trusted",
            lifecycle_status="archived",
        )
        session.add(archived)
        session.flush()
        session.add(
            KnowledgeCoverageGap(
                workspace_id=wsid,
                query_norm="archived coverage",
                surface="ask",
                tried_lanes=["keyword"],
                nearest_candidates=[
                    {
                        "id": str(archived.id),
                        "kind": "library_item",
                        "title": "Archived Coverage Source",
                    }
                ],
                status="open",
            )
        )
        # A corrupted scalar nearest_candidates must not 500.
        session.add(
            KnowledgeCoverageGap(
                workspace_id=wsid,
                query_norm="corrupt candidates row",
                surface="ask",
                tried_lanes=["keyword"],
                nearest_candidates=42,
                status="open",
            )
        )

    owner = ActorContext(
        actor_kind="system",
        actor_id="owner",
        scopes=("*",),
        workspace_id=wsid,
        workspace_role="owner",
        role="owner",
    )
    with factory() as session:
        gaps = list_coverage_gaps(session, workspace_id=wsid, actor=owner)

    for item in gaps.items:
        assert item.nearest_candidates == []


def test_coverage_candidate_rejects_superseded_library_version():
    # Codex round-12: a library_item_version/artifact candidate that points at a
    # superseded version must fail closed once a newer version replaces it, even
    # for an owner (the stored candidate carries the old snapshot title/path).
    from backend.app.db import make_engine, make_session_factory
    from backend.models.base import Base
    from backend.models.library import LibraryItem, LibraryItemVersion
    from backend.policy.auth import ActorContext

    engine = make_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = make_session_factory(engine)
    with factory.begin() as session:
        wsid = seed_default_workspace(session).id
        item = LibraryItem(
            workspace_id=wsid,
            connector_type="upload",
            format_hint="pdf",
            uri="https://example.com/cov-supersede.pdf",
            title="Coverage Supersede",
            trust_level="trusted",
            lifecycle_status="registered",
        )
        session.add(item)
        session.flush()
        v1 = LibraryItemVersion(
            workspace_id=wsid,
            library_item_id=item.id,
            version_no=1,
            content_hash="h1",
            raw_storage_ref="inline://r1",
        )
        v2 = LibraryItemVersion(
            workspace_id=wsid,
            library_item_id=item.id,
            version_no=2,
            content_hash="h2",
            raw_storage_ref="inline://r2",
            processing_status="completed",
        )
        session.add_all([v1, v2])
        session.flush()
        item.current_version_id = v2.id
        item.current_ready_version_id = v2.id
        session.flush()
        v1_id, v2_id = str(v1.id), str(v2.id)

    owner = ActorContext(
        actor_kind="system",
        actor_id="owner",
        scopes=("*",),
        workspace_id=wsid,
        workspace_role="owner",
        role="owner",
    )
    with factory() as session:
        assert (
            coverage_candidate_visible(
                session, {"id": v2_id, "kind": "library_item_version"}, owner, wsid
            )
            is True
        )
        assert (
            coverage_candidate_visible(
                session, {"id": v1_id, "kind": "library_item_version"}, owner, wsid
            )
            is False
        )


def test_coverage_candidate_visible_fails_closed_on_unverifiable():
    # Codex round-5: malformed/unknown candidates that still carry a title must
    # fail closed.
    from backend.policy.auth import ActorContext

    actor = ActorContext(
        actor_kind="human",
        actor_id="viewer",
        token_kind="session",
        scopes=("read",),
        workspace_role="viewer",
        role="viewer",
    )
    assert coverage_candidate_visible(None, "a string, not a dict", actor) is False
    assert (
        coverage_candidate_visible(
            None, {"id": "not-a-uuid", "kind": "agent_page", "title": "SECRET"}, actor
        )
        is False
    )
    assert (
        coverage_candidate_visible(
            None,
            {
                "id": "00000000-0000-0000-0000-000000000001",
                "kind": "future_kind",
                "title": "SECRET",
            },
            actor,
        )
        is False
    )


def test_repeated_coverage_gap_updates_open_row_and_single_task(session_factory):
    with session_factory.begin() as session:
        workspace_id = session.scalar(
            select(Workspace.id).where(Workspace.slug == "default")
        )
        first = record_coverage_gap(
            session,
            workspace_id=workspace_id,
            query="  Missing   Concept  ",
            surface="search_rest",
            tried_lanes=["entity", "keyword"],
            nearest_candidates=[],
        )
        second = record_coverage_gap(
            session,
            workspace_id=workspace_id,
            query="missing concept",
            surface="search_mcp",
            tried_lanes=["keyword", "grep_wiki"],
            nearest_candidates=[{"title": "Near"}],
        )

        assert first is not None
        assert second is first
        assert second.hit_count == 2
        assert second.surface == "search_rest"
        assert second.tried_lanes == ["entity", "keyword", "grep_wiki"]
        assert second.nearest_candidates == [{"title": "Near"}]
        assert session.scalar(select(func.count(KnowledgeCoverageGap.id))) == 1
        assert session.scalar(select(func.count(ClientDelegatedTask.id))) == 1


def test_coverage_gap_ignores_normalized_queries_shorter_than_three(session_factory):
    with session_factory.begin() as session:
        workspace_id = session.scalar(
            select(Workspace.id).where(Workspace.slug == "default")
        )

        gap = record_coverage_gap(
            session,
            workspace_id=workspace_id,
            query="ab",
            surface="ask",
            tried_lanes=[],
            nearest_candidates=[],
        )

        assert gap is None
        assert session.scalar(select(func.count(KnowledgeCoverageGap.id))) == 0
        assert session.scalar(select(func.count(ClientDelegatedTask.id))) == 0


def test_successful_query_resolves_gap_and_expires_pending_task(session_factory):
    with session_factory.begin() as session:
        workspace_id = session.scalar(
            select(Workspace.id).where(Workspace.slug == "default")
        )
        gap = record_coverage_gap(
            session,
            workspace_id=workspace_id,
            query="Previously Missing Concept",
            surface="search_rest",
            tried_lanes=["entity", "keyword"],
            nearest_candidates=[],
        )
        task = session.scalar(
            select(ClientDelegatedTask).where(ClientDelegatedTask.target_id == gap.id)
        )
        task.status = "leased"
        task.lease_expires_at = gap.created_at
        session.flush()

        resolved = resolve_coverage_gap(
            session,
            workspace_id=workspace_id,
            query=" previously   missing concept ",
        )

        assert resolved == 1
        assert gap.status == "resolved"
        assert gap.resolved_at is not None
        assert task.status == "expired"
        assert task.lease_expires_at is None
        assert (
            resolve_coverage_gap(
                session,
                workspace_id=workspace_id,
                query="Previously Missing Concept",
            )
            == 0
        )


def test_concurrent_coverage_gap_upsert_keeps_one_open_row_and_task(tmp_path):
    engine = create_engine(
        f"sqlite+pysqlite:///{tmp_path / 'coverage-race.db'}",
        connect_args={"check_same_thread": False, "timeout": 10},
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory.begin() as session:
        workspace_id = seed_default_workspace(session).id

    barrier = Barrier(2, timeout=30)

    def record(surface: str):
        with factory.begin() as session:
            barrier.wait(timeout=5)
            gap = record_coverage_gap(
                session,
                workspace_id=workspace_id,
                query="Concurrent Missing Concept",
                surface=surface,
                tried_lanes=[surface],
                nearest_candidates=[],
            )
            assert gap is not None
            return gap.id

    with ThreadPoolExecutor(max_workers=2) as pool:
        gap_ids = list(pool.map(record, ("search_rest", "search_mcp")))

    with factory() as session:
        gap = session.scalar(select(KnowledgeCoverageGap))
        assert gap_ids == [gap.id, gap.id]
        assert gap.hit_count == 2
        assert session.scalar(select(func.count(KnowledgeCoverageGap.id))) == 1
        assert session.scalar(select(func.count(ClientDelegatedTask.id))) == 1


def test_coverage_gap_list_links_staged_finding(session_factory):
    with session_factory.begin() as session:
        workspace_id = session.scalar(
            select(Workspace.id).where(Workspace.slug == "default")
        )
        gap = record_coverage_gap(
            session,
            workspace_id=workspace_id,
            query="Missing linked concept",
            surface="search_rest",
            tried_lanes=["entity"],
            nearest_candidates=[],
        )
        task = session.scalar(
            select(ClientDelegatedTask).where(ClientDelegatedTask.target_id == gap.id)
        )
        finding_id = gap.id
        task.status = "staged"
        task.result_ref = f"lint_finding:{finding_id}"
        session.flush()

        result = list_coverage_gaps(session, workspace_id=workspace_id)

    assert result.items[0].finding_id == finding_id


def test_coverage_gap_resolved_alias_includes_triaged_and_reports_truncation(
    session_factory,
):
    with session_factory.begin() as session:
        workspace_id = session.scalar(
            select(Workspace.id).where(Workspace.slug == "default")
        )
        session.add_all(
            KnowledgeCoverageGap(
                workspace_id=workspace_id,
                query_norm=f"triaged-gap-{index}",
                surface="search_rest",
                tried_lanes=["entity"],
                nearest_candidates=[],
                status="triaged",
                hit_count=1,
            )
            for index in range(101)
        )
        session.add(
            KnowledgeCoverageGap(
                workspace_id=workspace_id,
                query_norm="still-open",
                surface="search_mcp",
                tried_lanes=["keyword"],
                nearest_candidates=[],
                status="open",
                hit_count=100,
            )
        )
        session.flush()

        result = list_coverage_gaps(
            session,
            workspace_id=workspace_id,
            status="resolved",
            limit=100,
        )

    assert len(result.items) == 100
    assert result.has_more is True
    assert {item.status for item in result.items} == {"triaged"}

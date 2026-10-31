"""Production page-link observation write-through and replay invariants."""

from uuid import uuid4

import pytest
from sqlalchemy import delete, func, select

from backend.app.container import DatabaseProvider
from backend.models.context import EdgeProjection, PageLink
from backend.models.graph import GraphHistoryCoverage, GraphObservation
from backend.models.workspace import Workspace
from backend.policy.auth import ActorContext
from backend.schemas.graph import (
    GraphBudget,
    GraphQueryRequest,
    GraphSeed,
    GraphTimeQuery,
)
from backend.services.graph_observation_service import GraphObservationService
from backend.services.graph_query_service import GraphQueryService
from backend.services.indexing.edge_registry import refresh_page_edge_projection
from backend.services.indexing.reindex import reindex_workspace
from backend.services.page_service import PageService

pytestmark = pytest.mark.integration


def _write(
    service: PageService,
    *,
    slug: str,
    title: str,
    body: str,
    previous=None,
):
    return service.write_page(
        workspace_slug="default",
        slug=slug,
        title=title,
        body_md=body,
        actor_kind="human",
        actor_id="graph-observation-test",
        actor_provenance="browser_session",
        base_version_id=(None if previous is None else previous.version_id),
        base_content_hash=(None if previous is None else previous.content_hash),
    )


def _appearance_request(seed_id):
    return GraphQueryRequest(
        seeds=[GraphSeed(kind="context_page", id=seed_id)],
        preset="linked-knowledge",
        edge_kinds=["wikilink"],
        layers=["knowledge"],
        time=GraphTimeQuery(mode="appearance"),
        budget=GraphBudget(nodes=10, edges=10),
    )


def _viewer():
    return ActorContext(
        actor_kind="human",
        actor_id=str(uuid4()),
        token_kind="session",
        scopes=(),
        workspace_role="viewer",
    )


def _source_observations(session, source_id):
    return list(
        session.scalars(
            select(GraphObservation)
            .where(
                GraphObservation.source_kind == "context_page",
                GraphObservation.source_id == source_id,
                GraphObservation.edge_kind == "wikilink",
            )
            .order_by(GraphObservation.workspace_sequence)
        ).all()
    )


def test_publish_link_deltas_replay_removed_edges_and_readd_idempotently(
    session_factory,
    page_service,
):
    target = _write(
        page_service,
        slug="observed-target",
        title="Observed Target",
        body="# Observed Target\n\nBody.",
    )
    rollback_target = _write(
        page_service,
        slug="rollback-target",
        title="Rollback Target",
        body="# Rollback Target\n\nBody.",
    )
    source = _write(
        page_service,
        slug="observed-source",
        title="Observed Source",
        body="# Observed Source\n\nSee [[observed-target]].",
    )

    with session_factory() as session:
        workspace_id = session.scalar(
            select(Workspace.id).where(Workspace.slug == "default")
        )
        created_rows = _source_observations(session, source.id)
        coverage = session.scalar(
            select(GraphHistoryCoverage).where(
                GraphHistoryCoverage.workspace_id == workspace_id,
                GraphHistoryCoverage.stream_kind == "context_page",
                GraphHistoryCoverage.stream_id == source.id,
            )
        )
    assert [row.operation for row in created_rows] == ["edge_created"]
    assert coverage is not None
    assert coverage.status == "partial"
    assert coverage.coverage_from == created_rows[0].occurred_at
    assert coverage.watermark == created_rows[0].occurred_at

    duplicate = _write(
        page_service,
        slug="observed-source",
        title="Observed Source",
        body="# Observed Source\n\nSee [[observed-target]].",
        previous=source,
    )
    assert duplicate.version_id == source.version_id
    with session_factory() as session:
        assert len(_source_observations(session, source.id)) == 1

    removed = _write(
        page_service,
        slug="observed-source",
        title="Observed Source",
        body="# Observed Source\n\nThe link was removed.",
        previous=source,
    )
    with session_factory() as session:
        removed_scene = GraphQueryService().query(
            session,
            workspace_id,
            request=_appearance_request(source.id),
            actor=_viewer(),
            usage_signals_enabled=True,
        )
        rows_after_remove = _source_observations(session, source.id)

    assert [row.operation for row in rows_after_remove] == [
        "edge_created",
        "edge_removed",
    ]
    assert {node.entity_id for node in removed_scene.nodes} == {source.id}
    assert removed_scene.edges == []
    assert removed_scene.facets.edge_kinds == ["wikilink"]
    edge_events = [event for event in removed_scene.timeline if event.edge_ids]
    assert [event.operation for event in edge_events] == [
        "edge_created",
        "edge_removed",
    ]
    assert edge_events[0].edge_snapshots[0].id == edge_events[0].edge_ids[0]
    assert [node.entity_id for node in edge_events[0].node_snapshots] == [target.id]
    assert edge_events[1].edge_snapshots == []
    assert edge_events[1].node_snapshots == []
    assert edge_events[1].edge_ids == edge_events[0].edge_ids
    assert all(
        event.evidence is not None
        and event.evidence.available is False
        and event.evidence.id is None
        and event.evidence.ref is None
        for event in edge_events
    )
    assert all(
        str(row.id) not in removed_scene.model_dump_json() for row in rows_after_remove
    )
    assert removed_scene.history.status == "partial"

    readded = _write(
        page_service,
        slug="observed-source",
        title="Observed Source",
        body="# Observed Source\n\nSee [[observed-target]] again.",
        previous=removed,
    )
    with session_factory() as session:
        active_scene = GraphQueryService().query(
            session,
            workspace_id,
            request=_appearance_request(source.id),
            actor=_viewer(),
            usage_signals_enabled=True,
        )
        rows_after_readd = _source_observations(session, source.id)

    assert [row.operation for row in rows_after_readd] == [
        "edge_created",
        "edge_removed",
        "edge_created",
    ]
    assert len(active_scene.edges) == 1
    active_edge_id = active_scene.edges[0].id
    active_events = [event for event in active_scene.timeline if event.edge_ids]
    assert [event.operation for event in active_events] == [
        "edge_created",
        "edge_removed",
        "edge_created",
    ]
    assert {event.edge_ids[0] for event in active_events} == {active_edge_id}
    assert all(
        event.edge_snapshots[0].id == active_edge_id
        for event in active_events
        if event.operation == "edge_created"
    )
    assert active_scene.capabilities.timeline is True
    assert active_scene.capabilities.export is True
    assert active_scene.capabilities.evidence is False
    assert active_scene.capabilities.as_of is False

    # A producer failure rolls its projection, observation rows, and workspace
    # sequence back together. Mutating the rebuildable PageLink here provides a
    # deterministic delta without committing an extra page version.
    rollback_session = session_factory()
    try:
        link = rollback_session.scalar(
            select(PageLink).where(
                PageLink.source_page_id == source.id,
                PageLink.source_version_id == readded.version_id,
            )
        )
        assert link is not None
        link.target_slug = "rollback-target"
        link.target_page_id = rollback_target.id
        refresh_page_edge_projection(
            rollback_session,
            workspace_id,
            source.id,
        )
        assert len(_source_observations(rollback_session, source.id)) == 5
        rollback_session.rollback()
    finally:
        rollback_session.close()

    with session_factory() as session:
        assert len(_source_observations(session, source.id)) == 3
        assert (
            session.scalar(
                select(Workspace.graph_observation_sequence).where(
                    Workspace.id == workspace_id
                )
            )
            == 3
        )
        projected_target = session.scalar(
            select(EdgeProjection.target_id).where(
                EdgeProjection.workspace_id == workspace_id,
                EdgeProjection.source_id == source.id,
                EdgeProjection.edge_kind == "wikilink",
            )
        )
    assert projected_target == target.id

    # Destroy only the rebuildable current projection. Reindex restores it but
    # must not manufacture another occurrence in the historical ledger.
    with session_factory.begin() as session:
        session.execute(
            delete(EdgeProjection).where(
                EdgeProjection.workspace_id == workspace_id,
                EdgeProjection.source_id == source.id,
                EdgeProjection.edge_kind == "wikilink",
            )
        )

    class _NoopIndexer:
        def rebuild(self, *, workspace_id):
            return {}

    provider = DatabaseProvider(None, session_factory=session_factory)
    reindex_workspace(provider, _NoopIndexer(), workspace_id)
    with session_factory() as session:
        assert len(_source_observations(session, source.id)) == 3
        assert (
            session.scalar(
                select(func.count())
                .select_from(EdgeProjection)
                .where(
                    EdgeProjection.workspace_id == workspace_id,
                    EdgeProjection.source_id == source.id,
                    EdgeProjection.target_id == target.id,
                    EdgeProjection.edge_kind == "wikilink",
                )
            )
            == 1
        )


def test_unchanged_edges_advance_history_coverage_without_revision_or_event(
    session_factory,
    page_service,
):
    _write(
        page_service,
        slug="coverage-target",
        title="Coverage Target",
        body="# Coverage Target\n\nBody.",
    )
    source = _write(
        page_service,
        slug="coverage-source",
        title="Coverage Source",
        body="# Coverage Source\n\n[[coverage-target]]\n\nFirst prose.",
    )
    with session_factory() as session:
        workspace_id = session.scalar(
            select(Workspace.id).where(Workspace.slug == "default")
        )
        revision_before = session.scalar(
            select(Workspace.edge_projection_version).where(
                Workspace.id == workspace_id
            )
        )
        observations_before = len(_source_observations(session, source.id))

    second = _write(
        page_service,
        slug="coverage-source",
        title="Coverage Source",
        body="# Coverage Source\n\n[[coverage-target]]\n\nSecond prose.",
        previous=source,
    )

    assert second.version_id != source.version_id
    with session_factory() as session:
        coverage = session.scalar(
            select(GraphHistoryCoverage).where(
                GraphHistoryCoverage.workspace_id == workspace_id,
                GraphHistoryCoverage.stream_kind == "context_page",
                GraphHistoryCoverage.stream_id == source.id,
            )
        )
        revision_after = session.scalar(
            select(Workspace.edge_projection_version).where(
                Workspace.id == workspace_id
            )
        )
        observations_after = len(_source_observations(session, source.id))

    assert coverage is not None
    assert coverage.status == "partial"
    assert coverage.watermark == second.created_at
    assert revision_after == revision_before
    assert observations_after == observations_before


def test_published_page_without_edges_still_establishes_partial_coverage(
    session_factory,
    page_service,
):
    page = _write(
        page_service,
        slug="coverage-no-edges",
        title="Coverage Without Edges",
        body="# Coverage Without Edges\n\nPlain prose.",
    )

    with session_factory() as session:
        workspace_id = session.scalar(
            select(Workspace.id).where(Workspace.slug == "default")
        )
        coverage = session.scalar(
            select(GraphHistoryCoverage).where(
                GraphHistoryCoverage.workspace_id == workspace_id,
                GraphHistoryCoverage.stream_kind == "context_page",
                GraphHistoryCoverage.stream_id == page.id,
            )
        )
        observations = _source_observations(session, page.id)

    assert coverage is not None
    assert coverage.status == "partial"
    assert coverage.coverage_from == page.created_at
    assert coverage.watermark == page.created_at
    assert observations == []


def test_write_through_preserves_independently_proven_complete_coverage(
    session_factory,
    page_service,
):
    _write(
        page_service,
        slug="complete-coverage-target",
        title="Complete Coverage Target",
        body="# Complete Coverage Target\n\nBody.",
    )
    source = _write(
        page_service,
        slug="complete-coverage-source",
        title="Complete Coverage Source",
        body="# Complete Coverage Source\n\n[[complete-coverage-target]]",
    )
    with session_factory.begin() as session:
        workspace_id = session.scalar(
            select(Workspace.id).where(Workspace.slug == "default")
        )
        first = _source_observations(session, source.id)[0]
        GraphObservationService().set_coverage(
            session,
            workspace_id,
            stream_kind="context_page",
            stream_id=source.id,
            basis="domain_events",
            status="complete",
            coverage_from=None,
            watermark=first.occurred_at,
            extractor_version="independently-proven.v1",
        )

    _write(
        page_service,
        slug="complete-coverage-source",
        title="Complete Coverage Source",
        body="# Complete Coverage Source\n\nLink removed.",
        previous=source,
    )
    with session_factory() as session:
        coverage = session.scalar(
            select(GraphHistoryCoverage).where(
                GraphHistoryCoverage.workspace_id == workspace_id,
                GraphHistoryCoverage.stream_kind == "context_page",
                GraphHistoryCoverage.stream_id == source.id,
            )
        )

    assert coverage is not None
    assert coverage.status == "complete"
    assert coverage.basis == "mixed"
    assert coverage.coverage_from is None
    assert coverage.extractor_version == "independently-proven.v1"

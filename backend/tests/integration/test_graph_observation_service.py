"""Graph observation append/idempotency and coverage contracts."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select

from backend.models.graph import GraphObservation
from backend.models.workspace import Workspace
from backend.services.graph_observation_service import GraphObservationService
from backend.services.read_errors import ReadValidationError

pytestmark = pytest.mark.integration


def _workspace_id(session):
    return session.scalar(select(Workspace.id).where(Workspace.slug == "default"))


def test_observation_record_is_idempotent_and_sequences_without_gaps(
    session_factory,
):
    service = GraphObservationService()
    occurred_at = datetime(2026, 8, 4, 1, 0, tzinfo=UTC)
    source_id = uuid4()
    target_id = uuid4()

    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        first = service.record(
            session,
            workspace_id,
            operation="edge_created",
            source_kind="context_page",
            source_id=source_id,
            target_kind="context_page",
            target_id=target_id,
            edge_kind="wikilink",
            occurred_at=occurred_at,
            idempotency_key="domain-event:123:edge-created",
        )
        retried = service.record(
            session,
            workspace_id,
            operation="edge_created",
            source_kind="context_page",
            source_id=source_id,
            target_kind="context_page",
            target_id=target_id,
            edge_kind="wikilink",
            occurred_at=occurred_at,
            idempotency_key="domain-event:123:edge-created",
        )
        second = service.record(
            session,
            workspace_id,
            operation="edge_removed",
            source_kind="context_page",
            source_id=source_id,
            target_kind="context_page",
            target_id=target_id,
            edge_kind="wikilink",
            occurred_at=occurred_at + timedelta(minutes=1),
            idempotency_key="domain-event:124:edge-removed",
        )

        assert retried.id == first.id
        assert first.workspace_sequence == 1
        assert second.workspace_sequence == 2
        assert len(first.idempotency_key) == 64
        assert "domain-event" not in first.idempotency_key

    with session_factory() as session:
        workspace_id = _workspace_id(session)
        rows = session.scalars(
            select(GraphObservation)
            .where(GraphObservation.workspace_id == workspace_id)
            .order_by(GraphObservation.workspace_sequence)
        ).all()
        workspace = session.get(Workspace, workspace_id)
        assert [row.workspace_sequence for row in rows] == [1, 2]
        assert workspace.graph_observation_sequence == 2


def test_observation_rejects_idempotency_conflict_and_invalid_edge(session_factory):
    service = GraphObservationService()
    occurred_at = datetime(2026, 8, 4, 1, 0, tzinfo=UTC)
    source_id = uuid4()

    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        service.record(
            session,
            workspace_id,
            operation="node_created",
            source_kind="context_page",
            source_id=source_id,
            occurred_at=occurred_at,
            idempotency_key="same-source-event",
        )
        with pytest.raises(ReadValidationError, match="different facts"):
            service.record(
                session,
                workspace_id,
                operation="action_occurred",
                source_kind="context_page",
                source_id=source_id,
                action_kind="edited",
                occurred_at=occurred_at,
                idempotency_key="same-source-event",
            )
        with pytest.raises(ReadValidationError, match="require a target"):
            service.record(
                session,
                workspace_id,
                operation="edge_created",
                source_kind="context_page",
                source_id=source_id,
                edge_kind="wikilink",
                occurred_at=occurred_at,
                idempotency_key="invalid-edge",
            )


def test_coverage_upsert_is_explicit_and_monotonic_in_shape(session_factory):
    service = GraphObservationService()
    stream_id = uuid4()
    start = datetime(2026, 8, 4, 1, 0, tzinfo=UTC)
    watermark = start + timedelta(hours=1)

    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        created = service.set_coverage(
            session,
            workspace_id,
            stream_kind="context_page",
            stream_id=stream_id,
            basis="observations",
            status="partial",
            extractor_version="graph-observation.v1",
            coverage_from=start,
            watermark=watermark,
        )
        updated = service.set_coverage(
            session,
            workspace_id,
            stream_kind="context_page",
            stream_id=stream_id,
            basis="mixed",
            status="complete",
            extractor_version="graph-observation.v2",
            coverage_from=start,
            watermark=watermark,
        )
        assert updated.id == created.id
        assert updated.status == "complete"
        assert updated.basis == "mixed"

        with pytest.raises(ReadValidationError, match="must not exceed"):
            service.set_coverage(
                session,
                workspace_id,
                stream_kind="context_page",
                stream_id=uuid4(),
                basis="observations",
                status="complete",
                extractor_version="graph-observation.v1",
                coverage_from=watermark,
                watermark=start,
            )

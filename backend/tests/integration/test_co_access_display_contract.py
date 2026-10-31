"""co_access carries its evidence: how many sessions, since when, until when."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select

from backend.models.context import EdgeProjection
from backend.models.mcp_chat import McpChatSession, McpChatToolCallArtifact
from backend.models.mcp_trace import MCPToolCall
from backend.models.workspace import MCPClient, Workspace
from backend.policy.auth import ActorContext
from backend.schemas.graph import GraphBudget, GraphQueryRequest, GraphSeed
from backend.services.graph_query_service import GraphQueryService
from backend.services.indexing.edge_registry import (
    rebuild_edge_projection,
    refresh_co_access_edge_projection,
)

pytestmark = pytest.mark.integration


def _workspace_id(session):
    return session.scalar(select(Workspace.id).where(Workspace.slug == "default"))


def _publish(page_service, slug: str):
    return page_service.write_page(
        workspace_slug="default",
        slug=slug,
        title=slug.replace("-", " ").title(),
        body_md=f"# {slug}\n\nBody.",
        actor_kind="human",
        actor_id="co-access-display",
        actor_provenance="browser_session",
    )


def _read_pair(session, workspace_id, left_id, right_id, *, at):
    """One conversation reads ``left`` and, a minute later, ``right``."""

    client = MCPClient(
        workspace_id=workspace_id,
        name=f"display-{uuid4()}",
        client_type="generic",
        scopes=[],
        status="active",
    )
    session.add(client)
    session.flush()
    chat = McpChatSession(
        workspace_id=workspace_id,
        client_id=client.id,
        mcp_session_id=str(uuid4()),
        started_at=at,
        last_activity_at=at,
    )
    session.add(chat)
    session.flush()
    for offset, page_id in enumerate((left_id, right_id)):
        call = MCPToolCall(
            client_id=client.id,
            workspace_id=workspace_id,
            request_id=uuid4(),
            tool_name="read",
            result_status="success",
        )
        session.add(call)
        session.flush()
        session.add(
            McpChatToolCallArtifact(
                workspace_id=workspace_id,
                chat_session_id=chat.id,
                mcp_tool_call_id=call.id,
                resource_kind="context_page",
                resource_id=page_id,
                verb="referenced",
                occurred_at=at + timedelta(minutes=offset),
            )
        )
    session.flush()


def test_co_access_keeps_its_support_and_first_observation(
    session_factory, page_service
):
    """Three sessions across twelve days become one edge that says so."""

    left = _publish(page_service, "display-left")
    right = _publish(page_service, "display-right")
    now = datetime.now(UTC).replace(microsecond=0)
    starts = [now - timedelta(days=days, minutes=2) for days in (12, 6, 0)]
    first_observed = starts[0] + timedelta(minutes=1)
    last_observed = starts[-1] + timedelta(minutes=1)
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        for start in starts:
            _read_pair(session, workspace_id, left.id, right.id, at=start)
        assert refresh_co_access_edge_projection(session, workspace_id, now=now) == 1
        row = session.scalar(
            select(EdgeProjection).where(
                EdgeProjection.workspace_id == workspace_id,
                EdgeProjection.edge_kind == "co_access",
            )
        )
        assert row is not None
        assert row.support_session_count == 3
        assert row.first_observed_at == first_observed
        assert row.occurred_at == last_observed

        # A full rebuild writes the same evidence, and no other kind has any.
        rebuild_edge_projection(session, workspace_id, usage_signals_enabled=True)
        rows = session.scalars(
            select(EdgeProjection).where(EdgeProjection.workspace_id == workspace_id)
        ).all()
        by_kind = {edge.edge_kind: edge for edge in rows}
        assert by_kind["co_access"].support_session_count == 3
        assert by_kind["co_access"].first_observed_at == first_observed
        assert all(
            edge.support_session_count is None and edge.first_observed_at is None
            for edge in rows
            if edge.edge_kind != "co_access"
        )

    with session_factory() as session:
        scene = GraphQueryService().query(
            session,
            workspace_id,
            request=GraphQueryRequest(
                seeds=[GraphSeed(kind="context_page", id=left.id)],
                edge_kinds=["co_access"],
                layers=["activity"],
                budget=GraphBudget(nodes=10, edges=10),
            ),
            actor=ActorContext(
                actor_kind="human",
                actor_id=str(uuid4()),
                token_kind="session",
                scopes=(),
                workspace_role="viewer",
            ),
            usage_signals_enabled=True,
        )

    assert [edge.edge_kind for edge in scene.edges] == ["co_access"]
    edge = scene.edges[0]
    assert edge.support_session_count == 3
    assert edge.first_observed_at == first_observed
    assert edge.occurred_at == last_observed


def test_an_older_supporting_session_moves_the_first_observation_back(
    session_factory,
):
    """A refresh recounts the support and keeps the earliest observation."""

    now = datetime.now(UTC).replace(microsecond=0)
    left_id, right_id = uuid4(), uuid4()
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        for days in (3, 2):
            _read_pair(
                session,
                workspace_id,
                left_id,
                right_id,
                at=now - timedelta(days=days),
            )
        refresh_co_access_edge_projection(session, workspace_id, now=now)
        # An older session raises the support and moves the first observation
        # back without touching the last one.
        _read_pair(
            session, workspace_id, left_id, right_id, at=now - timedelta(days=9)
        )
        refresh_co_access_edge_projection(session, workspace_id, now=now)
        row = session.scalar(
            select(EdgeProjection).where(EdgeProjection.edge_kind == "co_access")
        )

    assert row.support_session_count == 3
    assert row.first_observed_at == now - timedelta(days=9, minutes=-1)

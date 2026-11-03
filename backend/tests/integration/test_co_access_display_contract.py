"""co_access carries its evidence: how many sessions, since when, until when."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from backend.models.context import EdgeProjection
from backend.models.graph import CoAccessPair
from backend.models.job import Job
from backend.models.mcp_chat import (
    McpChatArtifactKind,
    McpChatArtifactVerb,
    McpChatSession,
    McpChatToolCallArtifact,
)
from backend.models.mcp_trace import MCPToolCall
from backend.models.workspace import MCPClient, Workspace
from backend.policy.auth import ActorContext
from backend.schemas.graph import GraphBudget, GraphQueryRequest, GraphSeed
from backend.services.co_access import rebuild_co_access_pairs
from backend.services.graph_query_service import GraphQueryService
from backend.services.indexing.edge_registry import (
    rebuild_edge_projection,
)
from backend.services.mcp_chat_service import McpChatService

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
            result_status="succeeded",
            started_at=at + timedelta(minutes=offset),
            completed_at=at + timedelta(minutes=offset),
        )
        session.add(call)
        session.flush()
        McpChatService().link_artifact(
            session=session,
            chat_session=chat,
            tool_call=call,
            resource_kind=McpChatArtifactKind.CONTEXT_PAGE,
            resource_id=page_id,
            verb=McpChatArtifactVerb.VIEWED,
        )
    session.flush()
    return client, chat


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
        assert rebuild_co_access_pairs(session, workspace_id) == 1
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
        rebuild_co_access_pairs(session, workspace_id)
        # An older session raises the support and moves the first observation
        # back without touching the last one.
        _read_pair(session, workspace_id, left_id, right_id, at=now - timedelta(days=9))
        rebuild_co_access_pairs(session, workspace_id)
        row = session.scalar(
            select(EdgeProjection).where(EdgeProjection.edge_kind == "co_access")
        )

    assert row.support_session_count == 3
    assert row.first_observed_at == now - timedelta(days=9, minutes=-1)


def test_view_trace_updates_lifetime_pairs_atomically_and_repairs_late_reads(
    session_factory, monkeypatch
):
    """One trace trajectory covers incremental support, late delivery and retry.

    The four delayed intervening reads are one window-boundary transition,
    not four input variants: together they move the original pair beyond the
    five-read adjacency window and require removing that chat's contribution.
    """
    from backend.app.settings import Settings
    from backend.services import co_access
    from backend.services.job_queue import JobQueueService
    from backend.worker.runtime import OneShotWorker

    projected_artifacts = []
    record_read = co_access.record_co_access_read

    def record_new_view(*args, **kwargs):
        projected_artifacts.append(kwargs["artifact_id"])
        return record_read(*args, **kwargs)

    monkeypatch.setattr(co_access, "record_co_access_read", record_new_view)
    left, right = sorted((uuid4(), uuid4()))
    now = datetime.now(UTC).replace(microsecond=0)
    service = McpChatService()
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        access_timestamp = session.get(Workspace, workspace_id).updated_at
        worker = OneShotWorker(job_queue=JobQueueService(), settings=Settings())

        def repair_pending():
            while (
                worker.dispatch_once(
                    session, workspace_id=workspace_id, job_types={"co_access_repair"}
                )
                is not None
            ):
                pass

        def pair():
            return session.scalar(
                select(CoAccessPair)
                .where(CoAccessPair.source_id == left, CoAccessPair.target_id == right)
                .execution_options(populate_existing=True)
            )

        def edge():
            return session.scalar(
                select(EdgeProjection)
                .where(
                    EdgeProjection.edge_kind == "co_access",
                    EdgeProjection.source_id == left,
                    EdgeProjection.target_id == right,
                )
                .execution_options(populate_existing=True)
            )

        def view(client, chat, resource, at, call=None):
            if call is None:
                call = MCPToolCall(
                    workspace_id=workspace_id,
                    client_id=client.id,
                    request_id=uuid4(),
                    tool_name="read",
                    result_status="succeeded",
                    started_at=at,
                    completed_at=at,
                )
                session.add(call)
                session.flush()
            service.link_artifact(
                session=session,
                chat_session=chat,
                tool_call=call,
                resource_kind=McpChatArtifactKind.CONTEXT_PAGE,
                resource_id=resource,
                verb=McpChatArtifactVerb.VIEWED,
            )
            return call

        _read_pair(session, workspace_id, left, right, at=now - timedelta(days=400))
        assert pair().support_session_count == 1
        assert edge() is None
        client, chat = _read_pair(session, workspace_id, left, right, at=now)
        assert pair().support_session_count == edge().weight == 2
        first = pair().first_observed_at
        assert first == now - timedelta(days=400, minutes=-1)

        with monkeypatch.context() as late_projection:
            replay = co_access._replay_conversation
            inline_replays = []

            def record_replay(*args, **kwargs):
                inline_replays.append(args[2].id)
                return replay(*args, **kwargs)

            late_projection.setattr(co_access, "_replay_conversation", record_replay)
            for seconds in (10, 20, 30, 40):
                view(client, chat, uuid4(), now + timedelta(seconds=seconds))
            assert inline_replays == []
        # Late reads preserve their source immediately; only worker repair
        # changes the derived relationship, never the MCP response itself.
        assert pair().support_session_count == edge().weight == 2
        assert (
            session.scalar(
                select(func.count(Job.id)).where(
                    Job.job_type == "co_access_repair", Job.status == "queued"
                )
            )
            == 4
        )
        repair_pending()
        assert pair().support_session_count == 1
        assert edge() is None

        call = view(client, chat, left, now + timedelta(seconds=70))
        assert pair().support_session_count == edge().support_session_count == 2
        assert edge().first_observed_at == first
        assert edge().occurred_at == now + timedelta(seconds=70)
        edge_id = edge().id
        projections_before_retry = len(projected_artifacts)
        view(client, chat, left, now, call=call)
        assert len(projected_artifacts) == projections_before_retry
        assert pair().support_session_count == 2
        assert edge().id == edge_id
        from backend.services.graph_cache import edge_version

        revision = edge_version(session, workspace_id)
        view(client, chat, left, now + timedelta(seconds=80))
        assert pair().support_session_count == 2
        assert edge().occurred_at == now + timedelta(seconds=80)
        assert edge_version(session, workspace_id) == revision
        session.refresh(session.get(Workspace, workspace_id))
        assert session.get(Workspace, workspace_id).updated_at == access_timestamp
        projections_before_retry = len(projected_artifacts)

        # A citation registration is a distinct action. Reuse this conversation
        # to prove that the shared insertion hook never invokes read projection
        # for it, even though legacy presentation classes include CITED.
        citation_call = MCPToolCall(
            workspace_id=workspace_id,
            client_id=client.id,
            request_id=uuid4(),
            tool_name="cite",
            result_status="succeeded",
            started_at=now,
            completed_at=now,
        )
        session.add(citation_call)
        session.flush()
        service.link_artifact(
            session=session,
            chat_session=chat,
            tool_call=citation_call,
            resource_kind=McpChatArtifactKind.CONTEXT_PAGE,
            resource_id=uuid4(),
            verb=McpChatArtifactVerb.CITED,
        )
        assert len(projected_artifacts) == projections_before_retry
        assert pair().support_session_count == 2

        # Budget exception: two malformed-history inputs exercise distinct
        # proof fields: VIEWED on a non-read command and on a failed read.
        # An incremental-hook spy alone cannot prove the persisted SQL/rebuild
        # boundary, so reuse this real lifecycle rather than add DB cases.
        # They remain auditable but cannot contribute merely by saying VIEWED.
        invalid_view_targets = []
        for tool_name, result_status in (("cite", "succeeded"), ("read", "failed")):
            invalid_target = uuid4()
            invalid_view_targets.append(invalid_target)
            invalid_call = MCPToolCall(
                workspace_id=workspace_id,
                client_id=client.id,
                request_id=uuid4(),
                tool_name=tool_name,
                result_status=result_status,
                started_at=now,
                completed_at=now,
            )
            session.add(invalid_call)
            session.flush()
            service.link_artifact(
                session=session,
                chat_session=chat,
                tool_call=invalid_call,
                resource_kind=McpChatArtifactKind.CONTEXT_PAGE,
                resource_id=invalid_target,
                verb=McpChatArtifactVerb.VIEWED,
            )
            assert len(projected_artifacts) == projections_before_retry

        with pytest.raises(RuntimeError, match="rollback view"):
            with session.begin_nested():
                _read_pair(
                    session, workspace_id, left, right, at=now + timedelta(days=1)
                )
                assert pair().support_session_count == 3
                raise RuntimeError("rollback view")
        assert pair().support_session_count == edge().weight == 2

        # Switching visibility cannot discard cumulative support, including
        # views captured while disabled; restoration needs only the counters.
        session.get(Workspace, workspace_id).usage_relation_signals_enabled = False
        session.flush()
        _read_pair(session, workspace_id, left, right, at=now + timedelta(days=2))
        assert pair().support_session_count == 3
        assert edge() is None
        from backend.services.co_access import sync_co_access_projection

        sync_co_access_projection(session, workspace_id, usage_signals_enabled=True)
        assert edge().weight == edge().support_session_count == 3
        rebuild_co_access_pairs(session, workspace_id)
        assert (
            session.scalar(
                select(CoAccessPair.id).where(
                    (CoAccessPair.source_id.in_(invalid_view_targets))
                    | (CoAccessPair.target_id.in_(invalid_view_targets))
                )
            )
            is None
        )

        # Failure isolation is part of this trace trajectory: two raw views
        # survive a derived-projection failure, and replay repairs them once.
        completed_repairs = session.scalar(
            select(func.count(Job.id)).where(
                Job.job_type == "co_access_repair", Job.status == "completed"
            )
        )
        raw_count = session.scalar(select(func.count(McpChatToolCallArtifact.id)))
        with monkeypatch.context() as failed_projection:

            def fail_derived(*args, **kwargs):
                raise RuntimeError("derived projection unavailable")

            failed_projection.setattr(co_access, "record_co_access_read", fail_derived)
            _read_pair(session, workspace_id, left, right, at=now + timedelta(days=3))
        assert (
            session.scalar(select(func.count(McpChatToolCallArtifact.id)))
            == raw_count + 2
        )
        assert pair().support_session_count == 3
        assert (
            session.scalar(
                select(func.count(Job.id)).where(
                    Job.job_type == "co_access_repair", Job.status == "queued"
                )
            )
            == 2
        )
        repair_pending()
        assert pair().support_session_count == 4
        assert (
            session.scalar(
                select(func.count(Job.id)).where(
                    Job.job_type == "co_access_repair", Job.status == "completed"
                )
            )
            == completed_repairs + 2
        )

        # Reuse this recovery trajectory to pin the rebuild cohort boundary.
        # Insert a new chat after the lock set is captured but before its source
        # query. Its first view is processed; the second persists for repair.
        # Neither can contribute to this rebuild, even if that query sees them.
        session.get(Workspace, workspace_id).usage_relation_signals_enabled = True
        session.flush()
        raced_left, raced_right = sorted((uuid4(), uuid4()))
        source_rows = co_access.co_read_rows
        late_chat_id = None

        def source_with_new_chat(*args, **kwargs):
            nonlocal late_chat_id
            if late_chat_id is None:
                calls = 0

                def defer_second_view(*read_args, **read_kwargs):
                    nonlocal calls
                    calls += 1
                    if calls == 2:
                        raise co_access.CoAccessRepairRequired("maintenance owns graph")
                    return record_read(*read_args, **read_kwargs)

                with monkeypatch.context() as new_chat_capture:
                    new_chat_capture.setattr(
                        co_access, "record_co_access_read", defer_second_view
                    )
                    _client, late_chat = _read_pair(
                        session,
                        workspace_id,
                        raced_left,
                        raced_right,
                        at=now + timedelta(days=4),
                    )
                late_chat_id = late_chat.id
            return source_rows(*args, **kwargs)

        with monkeypatch.context() as concurrent_rebuild:
            concurrent_rebuild.setattr(co_access, "co_read_rows", source_with_new_chat)
            rebuild_co_access_pairs(session, workspace_id)

        def raced_pair():
            return session.scalar(
                select(CoAccessPair)
                .where(
                    CoAccessPair.source_id == raced_left,
                    CoAccessPair.target_id == raced_right,
                )
                .execution_options(populate_existing=True)
            )

        assert late_chat_id is not None
        assert raced_pair() is None
        assert session.execute(
            select(
                McpChatToolCallArtifact.co_access_segment,
                McpChatToolCallArtifact.co_access_position,
            )
            .where(McpChatToolCallArtifact.chat_session_id == late_chat_id)
            .order_by(McpChatToolCallArtifact.occurred_at)
        ).all() == [(0, 0), (None, None)]
        repair_pending()
        assert raced_pair().support_session_count == 1
        assert pair().support_session_count == 4
        assert (
            session.scalar(
                select(EdgeProjection.id).where(
                    EdgeProjection.edge_kind == "co_access",
                    EdgeProjection.source_id == raced_left,
                    EdgeProjection.target_id == raced_right,
                )
            )
            is None
        )

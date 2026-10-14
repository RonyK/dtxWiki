"""The owner's usage-signal switch turns "read together" off everywhere.

Turning it off removes the co_access projection and the hot-path findings
that come from the same pairs; the MCP read records they are built from stay.
Turning it back on rebuilds them at the next trail tick.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, func, select

from backend.app.main import create_app
from backend.app.settings import Settings
from backend.mcp.router import dispatch
from backend.models.context import ContextPage, EdgeProjection
from backend.models.job import Job
from backend.models.mcp_chat import McpChatSession, McpChatToolCallArtifact
from backend.models.mcp_trace import MCPToolCall
from backend.models.workspace import MCPClient, Workspace
from backend.policy.auth import ActorContext
from backend.schemas.graph import GraphBudget, GraphQueryRequest, GraphSeed
from backend.services import trail_analysis
from backend.services.graph_query_service import GraphQueryService
from backend.services.indexing.edge_registry import rebuild_edge_projection
from backend.services.job_queue import JobQueueService
from backend.services.trail_analysis import run_trail_tick
from backend.tests.conftest import (
    authenticate_test_client_in_workspace,
    captured_log_messages,
)
from backend.worker.runtime import OneShotWorker

pytestmark = pytest.mark.integration

WARNING = "usage_relation_signals_disabled"


def _workspace_id(session) -> UUID:
    return session.scalar(select(Workspace.id).where(Workspace.slug == "default"))


def _publish(page_service, slug: str):
    return page_service.write_page(
        workspace_slug="default",
        slug=slug,
        title=slug.replace("-", " ").title(),
        body_md=f"# {slug}\n\nBody.",
        actor_kind="human",
        actor_id="usage-signals",
        actor_provenance="browser_session",
    )


def _read_pair(session, workspace_id, left_id, right_id, *, at):
    client = MCPClient(
        workspace_id=workspace_id,
        name=f"signals-{uuid4()}",
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


def _co_access_count(session, workspace_id) -> int:
    return session.scalar(
        select(func.count(EdgeProjection.id)).where(
            EdgeProjection.workspace_id == workspace_id,
            EdgeProjection.edge_kind == "co_access",
        )
    )


def _co_access_scene(session, workspace_id, seed_id, *, enabled):
    return GraphQueryService().query(
        session,
        workspace_id,
        request=GraphQueryRequest(
            seeds=[GraphSeed(kind="context_page", id=seed_id)],
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
        usage_signals_enabled=enabled,
    )


def _seed_read_together(session_factory, page_service, now):
    left = _publish(page_service, f"signals-left-{uuid4().hex[:6]}")
    right = _publish(page_service, f"signals-right-{uuid4().hex[:6]}")
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        for days in (4, 2):
            _read_pair(
                session,
                workspace_id,
                left.id,
                right.id,
                at=now - timedelta(days=days),
            )
    return workspace_id, left.id, right.id


def test_switching_signals_off_clears_co_access_and_back_on_rebuilds_it(
    session_factory, page_service, monkeypatch
):
    now = datetime.now(UTC).replace(microsecond=0)
    workspace_id, left_id, _right_id = _seed_read_together(
        session_factory, page_service, now
    )
    with session_factory.begin() as session:
        run_trail_tick(session, workspace_id, now=now, usage_signals_enabled=True)
        assert _co_access_count(session, workspace_id) == 1

    # Off: the Local Graph stops showing the edge at once, before any tick.
    with session_factory() as session:
        scene = _co_access_scene(session, workspace_id, left_id, enabled=False)
    assert scene.edges == []
    assert WARNING in scene.warnings

    def no_hot_paths(*_args, **_kwargs):
        raise AssertionError("hot paths are co-read pairs; the switch is off")

    monkeypatch.setattr(trail_analysis, "_stage_hot_paths", no_hot_paths)
    with session_factory.begin() as session:
        result = run_trail_tick(
            session, workspace_id, now=now, usage_signals_enabled=False
        )
        assert result["co_access_edges"] == 0
        assert result["hot_path_findings"] == 0
        assert _co_access_count(session, workspace_id) == 0
        # The records the pairs come from are not touched.
        assert (
            session.scalar(
                select(func.count(McpChatToolCallArtifact.id)).where(
                    McpChatToolCallArtifact.workspace_id == workspace_id
                )
            )
            == 4
        )
    monkeypatch.undo()

    with session_factory.begin() as session:
        run_trail_tick(session, workspace_id, now=now, usage_signals_enabled=True)
        assert _co_access_count(session, workspace_id) == 1
    with session_factory() as session:
        scene = _co_access_scene(session, workspace_id, left_id, enabled=True)
    assert WARNING not in scene.warnings
    assert [edge.edge_kind for edge in scene.edges] == ["co_access"]
    assert scene.edges[0].support_session_count == 2
    assert scene.edges[0].first_observed_at is not None


def test_a_rebuild_keeps_co_access_off_while_the_switch_is_off(
    session_factory, page_service
):
    now = datetime.now(UTC).replace(microsecond=0)
    workspace_id, _left_id, _right_id = _seed_read_together(
        session_factory, page_service, now
    )
    with session_factory.begin() as session:
        counts = rebuild_edge_projection(
            session, workspace_id, usage_signals_enabled=False
        )
        assert counts.get("co_access", 0) == 0
        assert _co_access_count(session, workspace_id) == 0

        counts = rebuild_edge_projection(
            session, workspace_id, usage_signals_enabled=True
        )
        assert counts["co_access"] == 1
        assert _co_access_count(session, workspace_id) == 1


def test_the_worker_tick_reads_the_owner_switch(session_factory, page_service):
    now = datetime.now(UTC).replace(microsecond=0)
    workspace_id, _left_id, _right_id = _seed_read_together(
        session_factory, page_service, now
    )
    queue = JobQueueService()
    worker = OneShotWorker(job_queue=queue, settings=Settings())

    def tick(key: str) -> None:
        with session_factory.begin() as session:
            queue.enqueue(
                session=session,
                workspace_id=workspace_id,
                job_type="trail_tick",
                trigger_type="scheduled",
                idempotency_key=key,
                target_ref={"scheduled_for": now.isoformat()},
            )
        with session_factory.begin() as session:
            result = worker.dispatch_once(
                session, workspace_id=workspace_id, job_types={"trail_tick"}
            )
        assert result is not None and result.status == "completed", result

    tick("signals-on")
    with session_factory() as session:
        assert _co_access_count(session, workspace_id) == 1

    with session_factory.begin() as session:
        session.get(Workspace, workspace_id).usage_relation_signals_enabled = False
    tick("signals-off")
    with session_factory() as session:
        assert _co_access_count(session, workspace_id) == 0


def _rest_client(page_service, *, role="owner", raise_server_exceptions=True):
    client = TestClient(
        create_app(
            settings=Settings(
                database_url="sqlite+pysqlite:///:memory:",
                session_secret="usage-signals-secret",
            ),
            page_service=page_service,
        ),
        raise_server_exceptions=raise_server_exceptions,
    )
    workspace_id = authenticate_test_client_in_workspace(
        client,
        page_service.session_factory,
        email="usage-signals@example.com",
        workspace_slug="default",
        role=role,
    )
    return client, UUID(str(workspace_id))


def _save_switch(client, workspace_id, *, route, value):
    url = f"/api/v1/workspaces/{workspace_id}/settings/policies"
    if route == "single":
        return client.patch(
            url,
            json={
                "group": "graph",
                "key": "usage_relation_signals_enabled",
                "value": value,
            },
        )
    policies = client.get(url)
    assert policies.status_code == 200, policies.text
    return client.patch(
        f"{url}/batch",
        json={
            "group": "graph",
            "expected_revision": policies.json()["data"]["graph"]["revision"],
            "changes": {"usage_relation_signals_enabled": value},
        },
    )


def _trail_jobs(session_factory, workspace_id):
    with session_factory() as session:
        return list(
            session.scalars(
                select(Job).where(
                    Job.workspace_id == workspace_id,
                    Job.job_type == "trail_tick",
                )
            ).all()
        )


@pytest.mark.parametrize("route", ["single", "batch"])
def test_policy_opt_out_queues_after_commit_and_clears_rest_and_mcp_neighbors(
    session_factory, page_service, mcp_default_actor, monkeypatch, route
):
    now = datetime.now(UTC).replace(microsecond=0)
    workspace_id, left_id, right_id = _seed_read_together(
        session_factory, page_service, now
    )
    client, _ = _rest_client(page_service)
    with session_factory.begin() as session:
        run_trail_tick(session, workspace_id, now=now, usage_signals_enabled=True)
        assert _co_access_count(session, workspace_id) == 1
        left_slug = session.get(ContextPage, left_id).slug
        # A prompt settings tick must not remove curated links or another
        # workspace's usage projection, or delete the read trail it derives.
        foreign_workspace_id = session.scalar(
            select(Workspace.id).where(Workspace.id != workspace_id)
        )
        assert foreign_workspace_id is not None
        session.add_all(
            [
                EdgeProjection(
                    workspace_id=workspace_id,
                    source_kind="context_page",
                    source_id=left_id,
                    target_kind="context_page",
                    target_id=right_id,
                    edge_kind="wikilink",
                    provenance="curated",
                    weight=1.0,
                    source_ref={},
                ),
                EdgeProjection(
                    workspace_id=foreign_workspace_id,
                    source_kind="context_page",
                    source_id=uuid4(),
                    target_kind="context_page",
                    target_id=uuid4(),
                    edge_kind="co_access",
                    provenance="usage",
                    weight=1.0,
                    source_ref={},
                ),
            ]
        )

    actor = mcp_default_actor(scopes=("mcp:read",), role="viewer")
    neighbors_url = (
        f"/api/v1/workspaces/{workspace_id}/context/pages/{left_id}/neighbors"
    )

    def mcp_neighbors():
        response = dispatch(
            "read_concept",
            {"workspace_id": str(workspace_id), "path": f"/context/{left_slug}.md"},
            actor=actor,
            page_service=page_service,
        )
        assert response.envelope.result_status == "succeeded"
        assert response.neighbors is not None
        return response.neighbors

    before = client.get(neighbors_url)
    assert before.status_code == 200, before.text
    assert [item["id"] for item in before.json()["data"]["co_access"]] == [
        str(right_id)
    ]
    assert [item.id for item in mcp_neighbors().co_access] == [right_id]

    services = client.app.state.container.services
    method = "update_policy" if route == "single" else "update_policy_batch"
    original_update = getattr(services.settings_svc, method)
    commit_seen = []
    policy_sessions = []

    def observe_policy_transaction(**kwargs):
        control_session = kwargs["control_session"]
        policy_sessions.append(control_session)
        event.listen(
            control_session,
            "after_commit",
            lambda _session: commit_seen.append(True),
            once=True,
        )
        return original_update(**kwargs)

    monkeypatch.setattr(services.settings_svc, method, observe_policy_transaction)
    original_enqueue = services.job_queue.enqueue

    def enqueue_after_commit(*args, **kwargs):
        assert commit_seen == [True]
        assert not policy_sessions[0].in_transaction()
        return original_enqueue(*args, **kwargs)

    monkeypatch.setattr(services.job_queue, "enqueue", enqueue_after_commit)
    saved = _save_switch(client, workspace_id, route=route, value=False)
    assert saved.status_code == 200, saved.text
    assert (
        saved.json()["data"]["graph"]["policy"]["usage_relation_signals_enabled"]
        is False
    )
    jobs = _trail_jobs(session_factory, workspace_id)
    assert [(job.status, job.trigger_type) for job in jobs] == [("queued", "settings")]
    assert jobs[0].idempotency_key.startswith(
        "usage-signals:" + saved.json()["data"]["graph"]["revision"] + ":"
    )

    worker = OneShotWorker(job_queue=services.job_queue, settings=Settings())
    with session_factory.begin() as session:
        result = worker.dispatch_once(
            session, workspace_id=workspace_id, job_types={"trail_tick"}
        )
    assert result is not None and result.status == "completed", result
    after = client.get(neighbors_url)
    assert after.status_code == 200, after.text
    assert after.json()["data"]["co_access"] == []
    assert mcp_neighbors().co_access == []
    assert [item.id for item in mcp_neighbors().outbound] == [right_id]
    with session_factory() as session:
        assert _co_access_count(session, workspace_id) == 0
        assert _co_access_count(session, foreign_workspace_id) == 1
        assert (
            session.scalar(
                select(func.count(McpChatToolCallArtifact.id)).where(
                    McpChatToolCallArtifact.workspace_id == workspace_id
                )
            )
            == 4
        )
        assert (
            session.get(Workspace, workspace_id).usage_relation_signals_enabled is False
        )


@pytest.mark.parametrize("route", ["single", "batch"])
def test_each_policy_toggle_can_queue_again_after_an_equal_revision_completed(
    session_factory, page_service, route
):
    """A content revision repeats on off/on/off; it is not a change identity."""
    now = datetime.now(UTC).replace(microsecond=0)
    workspace_id, _left_id, _right_id = _seed_read_together(
        session_factory, page_service, now
    )
    client, _ = _rest_client(page_service)
    worker = OneShotWorker(
        job_queue=client.app.state.container.services.job_queue,
        settings=Settings(),
    )
    revisions = []
    job_ids = []
    for enabled in (False, True, False):
        saved = _save_switch(client, workspace_id, route=route, value=enabled)
        assert saved.status_code == 200, saved.text
        revisions.append(saved.json()["data"]["graph"]["revision"])
        with session_factory.begin() as session:
            result = worker.dispatch_once(
                session, workspace_id=workspace_id, job_types={"trail_tick"}
            )
        assert result is not None and result.status == "completed", result
        job_ids.append(result.job_id)
        with session_factory() as session:
            assert _co_access_count(session, workspace_id) == (1 if enabled else 0)
    assert revisions[0] == revisions[2]
    assert len(set(job_ids)) == 3


@pytest.mark.parametrize("route", ["single", "batch"])
def test_policy_transaction_failure_never_queues_a_tick(
    session_factory, page_service, monkeypatch, route
):
    client, workspace_id = _rest_client(page_service, raise_server_exceptions=False)
    services = client.app.state.container.services
    method = "update_policy" if route == "single" else "update_policy_batch"
    original_update = getattr(services.settings_svc, method)

    def fail_control_commit(**kwargs):
        def fail_before_commit(_session):
            raise RuntimeError("test policy commit failure")

        event.listen(kwargs["control_session"], "before_commit", fail_before_commit)
        return original_update(**kwargs)

    monkeypatch.setattr(services.settings_svc, method, fail_control_commit)

    def never_enqueue(*_args, **_kwargs):
        pytest.fail("policy control commit failed, so no workspace job is authorized")

    monkeypatch.setattr(services.job_queue, "enqueue", never_enqueue)
    response = _save_switch(client, workspace_id, route=route, value=False)
    assert response.status_code == 500
    with session_factory() as session:
        assert (
            session.get(Workspace, workspace_id).usage_relation_signals_enabled is True
        )
    assert _trail_jobs(session_factory, workspace_id) == []


@pytest.mark.parametrize("route", ["single", "batch"])
@pytest.mark.parametrize("bad_value", [None, "false", 0])
def test_rejected_policy_value_never_queues_a_tick(
    session_factory, page_service, route, bad_value
):
    client, workspace_id = _rest_client(page_service)
    response = _save_switch(client, workspace_id, route=route, value=bad_value)
    assert response.status_code == 422, response.text
    with session_factory() as session:
        assert (
            session.get(Workspace, workspace_id).usage_relation_signals_enabled is True
        )
    assert _trail_jobs(session_factory, workspace_id) == []


@pytest.mark.parametrize("route", ["single", "batch"])
def test_non_owner_policy_opt_out_never_changes_signal_or_queues_a_tick(
    session_factory, page_service, route
):
    client, workspace_id = _rest_client(page_service, role="editor")
    response = _save_switch(client, workspace_id, route=route, value=False)
    assert response.status_code == 403, response.text
    with session_factory() as session:
        assert (
            session.get(Workspace, workspace_id).usage_relation_signals_enabled is True
        )
    assert _trail_jobs(session_factory, workspace_id) == []


@pytest.mark.parametrize("route", ["single", "batch"])
@pytest.mark.parametrize("access", ["unauthenticated", "other_workspace"])
def test_unauthorized_policy_opt_out_never_queues_a_tick(
    session_factory, page_service, route, access
):
    client, workspace_id = _rest_client(page_service)
    policies_url = f"/api/v1/workspaces/{workspace_id}/settings/policies"
    revision = client.get(policies_url).json()["data"]["graph"]["revision"]
    if access == "unauthenticated":
        client.cookies.clear()
        expected_status = 401
    else:
        with session_factory.begin() as session:
            foreign = Workspace(
                slug="isolated-signals-workspace",
                title="Isolated signals workspace",
                name="Isolated signals workspace",
                visibility="private",
                join_policy="invite_only",
            )
            session.add(foreign)
            session.flush()
            workspace_id = foreign.id
        policies_url = f"/api/v1/workspaces/{workspace_id}/settings/policies"
        expected_status = 404
    if route == "single":
        response = client.patch(
            policies_url,
            json={
                "group": "graph",
                "key": "usage_relation_signals_enabled",
                "value": False,
            },
        )
    else:
        response = client.patch(
            f"{policies_url}/batch",
            json={
                "group": "graph",
                "expected_revision": revision,
                "changes": {"usage_relation_signals_enabled": False},
            },
        )
    assert response.status_code == expected_status, response.text
    with session_factory() as session:
        assert (
            session.get(Workspace, workspace_id).usage_relation_signals_enabled is True
        )
    assert _trail_jobs(session_factory, workspace_id) == []


def test_single_map_and_other_policy_keys_do_not_queue_a_tick(
    session_factory, page_service
):
    client, workspace_id = _rest_client(page_service)
    policies_url = f"/api/v1/workspaces/{workspace_id}/settings/policies"
    for group, key, value in (
        ("graph", "workspace_map_enabled", True),
        ("workspace", "title", "Usage signal policy tests"),
    ):
        response = client.patch(
            policies_url, json={"group": group, "key": key, "value": value}
        )
        assert response.status_code == 200, response.text
    assert _trail_jobs(session_factory, workspace_id) == []


def test_stale_or_partially_invalid_batch_never_queues_a_tick(
    session_factory, page_service
):
    client, workspace_id = _rest_client(page_service)
    url = f"/api/v1/workspaces/{workspace_id}/settings/policies"
    revision = client.get(url).json()["data"]["graph"]["revision"]
    changed = client.patch(
        url,
        json={"group": "graph", "key": "workspace_map_enabled", "value": True},
    )
    assert changed.status_code == 200, changed.text
    stale = client.patch(
        f"{url}/batch",
        json={
            "group": "graph",
            "expected_revision": revision,
            "changes": {"usage_relation_signals_enabled": False},
        },
    )
    assert stale.status_code == 409, stale.text
    rejected = client.patch(
        f"{url}/batch",
        json={
            "group": "graph",
            "expected_revision": changed.json()["data"]["graph"]["revision"],
            "changes": {
                "usage_relation_signals_enabled": False,
                "workspace_map_enabled": "invalid",
            },
        },
    )
    assert rejected.status_code == 422, rejected.text
    with session_factory() as session:
        assert (
            session.get(Workspace, workspace_id).usage_relation_signals_enabled is True
        )
    assert _trail_jobs(session_factory, workspace_id) == []


@pytest.mark.parametrize("route", ["single", "batch"])
def test_enqueue_failure_does_not_undo_committed_policy(
    session_factory, page_service, monkeypatch, route
):
    client, workspace_id = _rest_client(page_service)
    queue = client.app.state.container.services.job_queue
    original_enqueue = queue.enqueue

    def fail_enqueue(*args, **kwargs):
        original_enqueue(*args, **kwargs)
        raise RuntimeError("test workspace queue failure")

    monkeypatch.setattr(queue, "enqueue", fail_enqueue)
    # CLI/other suite tests can disable this named logger or root propagation.
    # Capture it directly so transaction assertions do not depend on order.
    with captured_log_messages("backend.api.read") as messages:
        response = _save_switch(client, workspace_id, route=route, value=False)
    assert response.status_code == 200, response.text
    with session_factory() as session:
        assert (
            session.get(Workspace, workspace_id).usage_relation_signals_enabled is False
        )
    assert _trail_jobs(session_factory, workspace_id) == []
    assert messages == [
        "Could not queue a trail tick after the usage-signal switch changed"
    ]


def test_the_local_graph_route_reads_the_owner_switch(session_factory, page_service):
    now = datetime.now(UTC).replace(microsecond=0)
    workspace_id, left_id, _right_id = _seed_read_together(
        session_factory, page_service, now
    )
    with session_factory.begin() as session:
        run_trail_tick(session, workspace_id, now=now, usage_signals_enabled=True)
    client, _ = _rest_client(page_service)
    body = {
        "seeds": [{"kind": "context_page", "id": str(left_id)}],
        "edge_kinds": ["co_access"],
        "layers": ["activity"],
    }

    on = client.post(f"/api/v1/workspaces/{workspace_id}/graph/query", json=body)
    assert on.status_code == 200, on.text
    assert [edge["edge_kind"] for edge in on.json()["data"]["edges"]] == ["co_access"]

    with session_factory.begin() as session:
        session.get(Workspace, workspace_id).usage_relation_signals_enabled = False
    off = client.post(f"/api/v1/workspaces/{workspace_id}/graph/query", json=body)
    assert off.status_code == 200, off.text
    assert off.json()["data"]["edges"] == []
    assert WARNING in off.json()["data"]["warnings"]


def test_saving_the_switch_asks_for_a_prompt_trail_tick(page_service):
    """The change reaches every reader of the projection without a day's wait."""

    client, workspace_id = _rest_client(page_service)
    policies = client.get(f"/api/v1/workspaces/{workspace_id}/settings/policies")
    assert policies.status_code == 200, policies.text
    revision = policies.json()["data"]["graph"]["revision"]

    saved = client.patch(
        f"/api/v1/workspaces/{workspace_id}/settings/policies/batch",
        json={
            "group": "graph",
            "expected_revision": revision,
            "changes": {"usage_relation_signals_enabled": False},
        },
    )
    assert saved.status_code == 200, saved.text

    with page_service.session_factory() as session:
        jobs = session.scalars(
            select(Job).where(
                Job.workspace_id == workspace_id,
                Job.job_type == "trail_tick",
                Job.status == "queued",
            )
        ).all()
    assert [job.trigger_type for job in jobs] == ["settings"]

    # Saving the map switch alone does not.
    revision = saved.json()["data"]["graph"]["revision"]
    client.patch(
        f"/api/v1/workspaces/{workspace_id}/settings/policies/batch",
        json={
            "group": "graph",
            "expected_revision": revision,
            "changes": {"workspace_map_enabled": True},
        },
    )
    with page_service.session_factory() as session:
        assert (
            session.scalar(
                select(func.count(Job.id)).where(
                    Job.workspace_id == workspace_id, Job.job_type == "trail_tick"
                )
            )
            == 1
        )

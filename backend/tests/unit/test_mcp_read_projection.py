"""Served identities, private trace metadata, and reader-gated activity."""

import asyncio
import hashlib
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from sqlalchemy import event, select

from backend.models.audit import AuditEvent
from backend.models.context import ContextPage, ContextPageVersion
from backend.models.mcp_chat import (
    McpChatArtifactKind,
    McpChatArtifactVerb,
    McpChatToolCallArtifact,
)
from backend.models.mcp_trace import MCPToolCall
from backend.models.workspace import MCPClient, Workspace
from backend.policy.auth import ActorContext
from backend.services.mcp_chat_service import McpChatService
from backend.services.mcp_read_projection import (
    capture_served_reads,
    finish_served_read,
    project_mcp_activity,
    project_mcp_activity_batch,
    record_served_read,
    validate_read_projection,
)
from backend.services.mcp_trace_capture import persist_mcp_tool_trace

BASE = datetime(2026, 10, 20, 9, tzinfo=UTC)


def _snapshot(page_id=None, version_id=None, revision=2, status="editing"):
    page_id, version_id = page_id or uuid4(), version_id or uuid4()
    version = SimpleNamespace(
        id=version_id,
        body_md="served body SECRET",
        content_hash="a" * 64,
        version_status=status,
        editing_revision=revision,
    )
    with capture_served_reads() as snapshots:
        record_served_read("context_page", page_id, version)
    return finish_served_read(
        "read", {"target_type": "context_page", "target_id": str(page_id)}, snapshots
    )


def test_recorder_keeps_served_revision_after_mutation_and_only_allowlist():
    page_id = uuid4()
    version = SimpleNamespace(
        id=uuid4(),
        body_md="served SECRET",
        version_status="editing",
        editing_revision=4,
    )
    with capture_served_reads() as snapshots:
        record_served_read("context_page", page_id, version)
        version.body_md, version.editing_revision = "later body", 5
    projection = finish_served_read(
        "read_context_outline",
        {
            "target": {"path": "/SECRET"},
            "select": {"by": "heading_path", "heading_path": ["SECRET selector"]},
            "conversation_token": "TOKEN_SECRET",
        },
        snapshots,
    )
    assert projection["editing_revision"] == 4
    assert (
        projection["content_identity"] == hashlib.sha256(b"served SECRET").hexdigest()
    )
    assert "SECRET" not in json.dumps(projection)
    assert "TOKEN" not in json.dumps(projection)
    assert set(projection) == {
        "schema",
        "resource_kind",
        "resource_id",
        "version_id",
        "version_status",
        "content_identity",
        "scope_identity",
        "editing_revision",
    }
    projection["body"] = "untrusted"
    assert "body" not in validate_read_projection(projection)


def test_recorder_is_noop_outside_dispatch_and_nested_capture_isolated():
    page_id, version_id = uuid4(), uuid4()
    version = SimpleNamespace(
        id=version_id, body_md="body", version_status="published_snapshot"
    )
    record_served_read("context_page", page_id, version)
    with capture_served_reads() as outer:
        record_served_read("context_page", page_id, version)
        with capture_served_reads() as inner:
            record_served_read("context_page", uuid4(), version)
        record_served_read("context_page", page_id, version)
    assert len(outer) == len(inner) == 1
    assert outer[0]["resource_id"] != inner[0]["resource_id"]
    with pytest.raises(RuntimeError), capture_served_reads():
        raise RuntimeError("reset even on handler error")
    record_served_read("context_page", uuid4(), version)
    assert len(outer) == 1


def test_recorder_thread_dispatches_do_not_share_context():
    def dispatch_one():
        page_id = uuid4()
        version = SimpleNamespace(
            id=uuid4(), body_md="body", version_status="published_snapshot"
        )
        with capture_served_reads() as snapshots:
            record_served_read("context_page", page_id, version)
        return snapshots

    async def run():
        return await asyncio.gather(
            asyncio.to_thread(dispatch_one), asyncio.to_thread(dispatch_one)
        )

    first, second = asyncio.run(run())
    assert len(first) == len(second) == 1
    assert first[0]["resource_id"] != second[0]["resource_id"]


def test_scope_changes_split_reads_and_defaults_are_canonical():
    version = SimpleNamespace(
        id=uuid4(), body_md="body", version_status="published_snapshot"
    )
    with capture_served_reads() as snapshots:
        record_served_read("context_page", uuid4(), version)
    default = finish_served_read("read_concept", {}, snapshots)
    explicit_default = finish_served_read(
        "read_concept",
        {"format": "structured", "as_of": None, "include_neighbors": True},
        snapshots,
    )
    changed = finish_served_read(
        "read_concept", {"include_neighbors": False}, snapshots
    )
    assert default == explicit_default
    assert changed["scope_identity"] != default["scope_identity"]
    assert finish_served_read("search", {}, snapshots) is None
    assert (
        finish_served_read(
            "read", {}, snapshots + [{**snapshots[0], "resource_id": str(uuid4())}]
        )
        is None
    )
    assert validate_read_projection({**_snapshot(), "editing_revision": True}) is None


def _seed(session):
    workspace = session.scalar(select(Workspace).where(Workspace.slug == "default"))
    client = session.scalar(
        select(MCPClient).where(MCPClient.workspace_id == workspace.id)
    )
    page = ContextPage(
        workspace_id=workspace.id,
        slug=f"projection-{uuid4().hex}",
        title="Visible read title",
        page_type="overview",
        lifecycle_status="published",
    )
    session.add(page)
    session.flush()
    version = ContextPageVersion(
        workspace_id=workspace.id,
        page_id=page.id,
        version_no=1,
        body_md="# Stable body",
        content_hash="a" * 64,
        actor_type="human",
        created_by_actor_kind="human",
        created_by_actor_id="author",
        version_status="published_snapshot",
        last_saved_at=BASE - timedelta(days=1),
    )
    session.add(version)
    session.flush()
    page.current_published_version_id = page.current_version_id = version.id
    session.add(
        AuditEvent(
            workspace_id=workspace.id,
            actor_kind="human",
            actor_id="author",
            action="context_page.draft.publish",
            target_kind="context_page",
            target_id=page.id,
            payload={"version_id": str(version.id)},
            created_at=BASE - timedelta(days=1),
        )
    )
    session.flush()
    actor = ActorContext(
        actor_kind="human",
        actor_id="reader",
        workspace_id=workspace.id,
        role="viewer",
        workspace_role="viewer",
        scopes=("read",),
    )
    return workspace, client, page, version, actor


def _call(
    session,
    workspace,
    client,
    page,
    version,
    *,
    snapshot=True,
    status="succeeded",
    tool="read",
    chat_token=None,
):
    call = MCPToolCall(
        workspace_id=workspace.id,
        client_id=client.id,
        mcp_session_id=chat_token or uuid4(),
        request_id=uuid4(),
        tool_name=tool,
        input_summary={
            "target_type": "context_page_version",
            "target_id": str(version.id),
        },
        result_status=status,
        result_detail={
            "read_projection": _snapshot(
                page.id, version.id, status=version.version_status
            )
        }
        if snapshot
        else None,
        result_summary="SECRET raw result",
        error_code=None,
        latency_ms=12,
        started_at=BASE,
        completed_at=BASE,
    )
    session.add(call)
    session.flush()
    service = McpChatService()
    chat = service.upsert_session_from_tool_call(
        session=session,
        workspace_id=workspace.id,
        client_id=client.id,
        mcp_session_id=str(call.mcp_session_id),
        tool_call=call,
    )
    service.link_artifact(
        session=session,
        chat_session=chat,
        tool_call=call,
        resource_kind=McpChatArtifactKind.CONTEXT_PAGE,
        resource_id=page.id,
        verb=McpChatArtifactVerb.REFERENCED,
    )
    session.flush()
    return call, chat


def test_projection_uses_single_call_edge_and_hides_private_version(session_factory):
    with session_factory.begin() as session:
        workspace, client, page, version, actor = _seed(session)
        call, chat = _call(session, workspace, client, page, version)
        result = project_mcp_activity(session, call, actor)
        assert result.visible and result.grouping_key is not None
        assert result.resource_id == page.id
        assert result.navigation_path == (
            f"/workspaces/{page.workspace_id}"
            f"/context/pages/{page.id}"
        )
        assert result.operation == "read" and result.result == "succeeded"
        assert result.duration_ms == 12 and result.public_error_code is None
        assert "SECRET" not in repr(result)
        call.error_code = "SECRET_ERROR"
        errored = project_mcp_activity(session, call, actor)
        assert errored.public_error_code is None and errored.grouping_key is None
        call.error_code = None
        private = ContextPageVersion(
            workspace_id=workspace.id,
            page_id=page.id,
            version_no=2,
            body_md="private",
            content_hash="b" * 64,
            actor_type="human",
            created_by_actor_kind="human",
            created_by_actor_id="another-author",
            version_status="editing",
            editing_revision=3,
        )
        session.add(private)
        session.flush()
        call.input_summary = {"target_type": "context_page", "target_id": str(page.id)}
        call.result_detail = {
            "read_projection": _snapshot(page.id, private.id, revision=3)
        }
        assert project_mcp_activity(session, call, actor).visible is False
        # A separately public page head cannot expose its private saved version.
        call.result_detail = {"version_id": str(private.id)}
        assert project_mcp_activity(session, call, actor).visible is False
        # Internal metadata does not enter the existing Chats wire serializer.
        call.result_detail = {
            "read_projection": _snapshot(
                page.id, version.id, status="published_snapshot"
            )
        }
        detail = McpChatService().read_chat(
            session=session,
            workspace_id=workspace.id,
            chat_session_id=chat.id,
            actor=actor,
        )
        assert "read_projection" not in detail.model_dump_json()


@pytest.mark.parametrize("shared_status", ["draft", "published_snapshot"])
def test_captured_private_edit_does_not_become_shared_with_later_revision(
    session_factory, shared_status
):
    with session_factory.begin() as session:
        workspace, client, page, published, viewer = _seed(session)
        version = ContextPageVersion(
            workspace_id=workspace.id,
            page_id=page.id,
            version_no=2,
            body_md="private revision three",
            content_hash="b" * 64,
            actor_type="human",
            created_by_actor_kind="human",
            created_by_actor_id="editing-author",
            version_status="editing",
            editing_revision=3,
        )
        session.add(version)
        session.flush()
        author = ActorContext(
            actor_kind="human",
            actor_id="editing-author",
            workspace_id=workspace.id,
            workspace_role="editor",
            role="editor",
            scopes=("read",),
        )
        other_editor = ActorContext(
            actor_kind="human",
            actor_id="other-editor",
            workspace_id=workspace.id,
            workspace_role="editor",
            role="editor",
            scopes=("read",),
        )
        call, _ = _call(session, workspace, client, page, published)
        call.input_summary = {"target_type": "context_page", "target_id": str(page.id)}
        with capture_served_reads() as snapshots:
            record_served_read("context_page", page.id, version)
        call.result_detail = {
            "read_projection": finish_served_read("read", call.input_summary, snapshots)
        }
        assert project_mcp_activity(session, call, author).grouping_key is not None
        assert not project_mcp_activity(session, call, other_editor).visible
        assert not project_mcp_activity(session, call, viewer).visible

        # Only the final revision is deliberately submitted/shared. The older
        # private snapshot in result_detail must retain its original policy.
        if shared_status == "published_snapshot":
            published.version_status = "superseded_snapshot"
            session.flush()
            page.current_published_version_id = version.id
        version.body_md = "different shared revision four"
        version.editing_revision = 4
        version.version_status = shared_status
        page.current_version_id = version.id
        page.latest_draft_version_id = version.id if shared_status == "draft" else None
        session.flush()
        assert project_mcp_activity(session, call, author).grouping_key is not None
        for reader in (viewer, other_editor):
            projection = project_mcp_activity(session, call, reader)
            assert projection.visible is False
            assert projection.grouping_key is None and projection.resource_id is None
            assert projection.title is None and projection.navigation_path is None

        # Losing draft-read capability also hides the author's own historical
        # private read; being the recorded author alone is insufficient.
        downgraded_author = ActorContext(
            actor_kind="human",
            actor_id="editing-author",
            workspace_id=workspace.id,
            workspace_role="viewer",
            role="viewer",
            scopes=("read",),
        )
        assert not project_mcp_activity(session, call, downgraded_author).visible


def test_served_draft_stays_with_draft_readers_after_publication_in_place(
    session_factory,
):
    with session_factory.begin() as session:
        workspace, client, page, published, viewer = _seed(session)
        draft = ContextPageVersion(
            workspace_id=workspace.id,
            page_id=page.id,
            version_no=2,
            body_md="pre-publication draft",
            content_hash="b" * 64,
            actor_type="human",
            created_by_actor_kind="human",
            created_by_actor_id="author",
            version_status="draft",
        )
        session.add(draft)
        session.flush()
        call, _ = _call(session, workspace, client, page, draft)
        editor = ActorContext(
            actor_kind="human",
            actor_id="editor",
            workspace_id=workspace.id,
            role="editor",
            workspace_role="editor",
            scopes=("read",),
        )
        assert project_mcp_activity(session, call, editor).visible
        assert not project_mcp_activity(session, call, viewer).visible
        published.version_status = "superseded_snapshot"
        session.flush()
        draft.version_status = "published_snapshot"
        session.flush()
        assert not project_mcp_activity(session, call, viewer).visible


@pytest.mark.parametrize(
    "status",
    [
        "failed",
        "partial",
        "accepted",
        "needs_review",
        "conflict",
        "rejected_by_policy",
        "future",
    ],
)
def test_non_success_never_groups(session_factory, status):
    with session_factory.begin() as session:
        workspace, client, page, version, actor = _seed(session)
        call, _ = _call(session, workspace, client, page, version, status=status)
        assert project_mcp_activity(session, call, actor).grouping_key is None


def test_legacy_requires_pre_call_frozen_identity_and_not_rollup(session_factory):
    with session_factory.begin() as session:
        workspace, client, page, version, actor = _seed(session)
        call, _ = _call(session, workspace, client, page, version, snapshot=False)
        assert project_mcp_activity(session, call, actor).grouping_key is not None
        version.last_saved_at = BASE + timedelta(seconds=1)
        assert project_mcp_activity(session, call, actor).grouping_key is None
        version.last_saved_at = BASE - timedelta(days=1)
        call.started_at = BASE - timedelta(days=2)
        version.last_saved_at = BASE - timedelta(days=3)
        # Last save precedes the old call, but publication still follows it.
        assert project_mcp_activity(session, call, actor).grouping_key is None
        call.started_at = BASE
        call.input_summary = {"target_type": "context_page", "target_id": str(page.id)}
        unqualified = project_mcp_activity(session, call, actor)
        assert unqualified.grouping_key is None
        assert unqualified.resource_id is None and unqualified.title is None
        # Rollups remain present but cannot create a per-call target/group.
        for edge in session.scalars(
            select(McpChatToolCallArtifact).where(
                McpChatToolCallArtifact.mcp_tool_call_id == call.id
            )
        ):
            session.delete(edge)
        session.flush()
        result = project_mcp_activity(session, call, actor)
        assert result.grouping_key is None and result.resource_id is None


def test_mismatched_chat_and_multi_target_do_not_group(session_factory):
    with session_factory.begin() as session:
        workspace, client, page, version, actor = _seed(session)
        call, chat = _call(session, workspace, client, page, version)
        chat.mcp_session_id = str(uuid4())
        assert project_mcp_activity(session, call, actor).grouping_key is None
        chat.mcp_session_id = str(call.mcp_session_id)
        other = ContextPage(
            workspace_id=workspace.id,
            slug=f"other-{uuid4().hex}",
            title="Other",
            page_type="overview",
        )
        session.add(other)
        session.flush()
        # Even a second edge to a hidden resource suppresses the entire call.
        McpChatService().link_artifact(
            session=session,
            chat_session=chat,
            tool_call=call,
            resource_kind=McpChatArtifactKind.CONTEXT_PAGE,
            resource_id=other.id,
            verb=McpChatArtifactVerb.REFERENCED,
        )
        session.flush()
        assert project_mcp_activity(session, call, actor).visible is False


def test_persistence_accepts_only_separate_allowlisted_internal_metadata(
    page_service, mcp_default_actor
):
    actor = mcp_default_actor(scopes=("read",))
    snapshot = _snapshot(status="published_snapshot")
    snapshot["body"] = "SECRET body"
    payload = {
        "envelope": {
            "workspace_id": str(actor.workspace_id),
            "request_id": str(uuid4()),
            "result_status": "succeeded",
        },
        "read_projection": {"evil": "SECRET wire injection"},
    }
    with page_service.session_factory.begin() as session:
        call = persist_mcp_tool_trace(
            session=session,
            actor=actor,
            tool_name="read",
            arguments={},
            payload=payload,
            mcp_session_id=str(uuid4()),
            started_at=BASE,
            completed_at=BASE,
            latency_ms=1,
            create_chat_session=False,
            read_projection=snapshot,
        )
        assert set(call.result_detail) == {"read_projection"}
        assert "SECRET" not in json.dumps(call.result_detail)
        failed = persist_mcp_tool_trace(
            session=session,
            actor=actor,
            tool_name="read",
            arguments={},
            payload={
                **payload,
                "envelope": {
                    **payload["envelope"],
                    "request_id": str(uuid4()),
                    "result_status": "partial",
                },
            },
            mcp_session_id=str(uuid4()),
            started_at=BASE,
            completed_at=BASE,
            latency_ms=1,
            create_chat_session=False,
            read_projection=snapshot,
        )
        assert failed.result_detail is None


def test_dispatch_snapshots_actual_read_and_keeps_internal_attribute_off_wire(
    page_service, mcp_default_actor
):
    from backend.mcp.router import dispatch

    actor = mcp_default_actor(scopes=("read",))
    with page_service.session_factory.begin() as session:
        workspace, _client, page, version, _actor = _seed(session)
        page_id, version_id = page.id, version.id
    response = dispatch(
        "read",
        {
            "workspace_id": str(workspace.id),
            "target_type": "context_page_version",
            "target_id": str(version_id),
        },
        actor=actor,
        page_service=page_service,
    )
    projection = response._served_read_projection
    assert projection["resource_id"] == str(page_id)
    assert projection["version_id"] == str(version_id)
    assert "read_projection" not in response.model_dump_json()
    assert "_served_read_projection" not in response.model_dump()


@pytest.mark.parametrize(
    "tool",
    ["read", "fetch", "read_context_outline", "read_structure_map"],
)
def test_all_context_read_handlers_capture_exact_editing_revision(
    page_service, mcp_default_actor, tool
):
    from backend.mcp.router import dispatch

    actor = mcp_default_actor(scopes=("read", "draft-write"))
    with page_service.session_factory.begin() as session:
        workspace, _client, page, published, _actor = _seed(session)
        version = ContextPageVersion(
            workspace_id=workspace.id,
            page_id=page.id,
            version_no=2,
            body_md="# Editing\n\nserved revision seven",
            content_hash="c" * 64,
            actor_type=actor.actor_kind,
            created_by_actor_kind=actor.actor_kind,
            created_by_actor_id=actor.actor_id,
            version_status="editing",
            editing_revision=7,
        )
        session.add(version)
        session.flush()
        page.latest_draft_version_id = version.id
        from backend.services.page_service import _sync_page_name_index

        _sync_page_name_index(session, page=page)
        page_id, version_id, slug = page.id, version.id, page.slug
    target = {
        "read": {"target_type": "context_page_version", "target_id": str(version_id)},
        "fetch": {"id": str(page_id), "type": "context_page"},
        "read_concept": {"path": f"/context/{slug}.md", "include_neighbors": False},
        "read_context_outline": {"target": {"version_id": str(version_id)}},
        "read_structure_map": {"target": {"version_id": str(version_id)}},
    }[tool]
    response = dispatch(
        tool,
        {"workspace_id": str(workspace.id), **target},
        actor=actor,
        page_service=page_service,
    )
    assert response.envelope.result_status == "succeeded"
    projection = response._served_read_projection
    assert projection["version_id"] == str(version_id)
    assert projection["editing_revision"] == 7
    assert (
        projection["content_identity"]
        == hashlib.sha256(b"# Editing\n\nserved revision seven").hexdigest()
    )
    assert "read_projection" not in response.model_dump_json()


def test_agent_slug_read_runner_persists_private_metadata(
    page_service, mcp_default_actor
):
    from backend.mcp.protocol.runner import call_tool_async
    from backend.models.agent import AgentPage, AgentPageVersion

    actor = mcp_default_actor(scopes=("read",))
    with page_service.session_factory.begin() as session:
        page = AgentPage(
            workspace_id=actor.workspace_id,
            category="general",
            slug=f"agent-{uuid4().hex}",
            title="Agent served",
            lifecycle_status="active",
            sensitivity="workspace",
        )
        session.add(page)
        session.flush()
        version = AgentPageVersion(
            workspace_id=actor.workspace_id,
            agent_page_id=page.id,
            version_no=1,
            body_markdown="agent body SECRET",
            actor_type="human",
            version_status="active",
        )
        session.add(version)
        session.flush()
        page.current_active_version_id = version.id
        slug, page_id, version_id = page.slug, page.id, version.id
    result = asyncio.run(
        call_tool_async(
            "read_agent_page",
            {
                "workspace_id": str(actor.workspace_id),
                "slug": slug,
                "conversation_token": "new",
            },
            actor=actor,
            page_service=page_service,
        )
    )
    assert not result.isError
    assert "read_projection" not in json.dumps(result.structuredContent)
    with page_service.session_factory() as session:
        call = session.scalar(
            select(MCPToolCall)
            .where(MCPToolCall.tool_name == "read_agent_page")
            .order_by(MCPToolCall.started_at.desc())
        )
        assert call.result_detail["read_projection"]["version_id"] == str(version_id)
        assert "SECRET" not in json.dumps(call.result_detail)
        projection = project_mcp_activity(session, call, actor)
        assert projection.resource_id == page_id and projection.grouping_key is not None


def test_concept_handler_snapshots_its_resolved_published_version(
    page_service, mcp_default_actor
):
    from backend.mcp.router import dispatch
    from backend.services.page_service import _sync_page_name_index

    actor = mcp_default_actor(scopes=("read",))
    with page_service.session_factory.begin() as session:
        workspace, _client, page, version, _actor = _seed(session)
        _sync_page_name_index(session, page=page)
        slug, version_id = page.slug, version.id
    response = dispatch(
        "read_concept",
        {
            "workspace_id": str(workspace.id),
            "path": f"/context/{slug}.md",
            "include_neighbors": False,
        },
        actor=actor,
        page_service=page_service,
    )
    assert response.envelope.result_status == "succeeded"
    assert response._served_read_projection["version_id"] == str(version_id)
    assert "read_projection" not in response.model_dump_json()


def test_group_key_separates_version_scope_session_client_and_tool(session_factory):
    with session_factory.begin() as session:
        workspace, client, page, version, actor = _seed(session)
        call, chat = _call(session, workspace, client, page, version)
        original = project_mcp_activity(session, call, actor).grouping_key
        read = call.result_detail["read_projection"]
        call.result_detail = {"read_projection": {**read, "scope_identity": "b" * 64}}
        assert project_mcp_activity(session, call, actor).grouping_key != original
        call.result_detail = {"read_projection": read}
        call.result_detail = {"read_projection": {**read, "content_identity": "c" * 64}}
        assert project_mcp_activity(session, call, actor).grouping_key != original
        call.result_detail = {"read_projection": read}
        call.tool_name = "read_context_outline"
        assert project_mcp_activity(session, call, actor).grouping_key != original
        for tool in (
            "search",
            "update_context_edit",
            "begin_context_edit",
            "list_changes",
            "read_page",
            "unknown_read_thing",
        ):
            call.tool_name = tool
            assert project_mcp_activity(session, call, actor).grouping_key is None
        call.tool_name = "read"
        call.mcp_session_id = uuid4()
        assert project_mcp_activity(session, call, actor).grouping_key is None
        call.mcp_session_id = UUID(chat.mcp_session_id)
        second_client = MCPClient(workspace_id=workspace.id, name="Other client")
        session.add(second_client)
        session.flush()
        call.client_id = second_client.id
        assert project_mcp_activity(session, call, actor).grouping_key != original


def test_verified_conversation_can_group_new_client_calls_after_client_switch(
    session_factory,
):
    with session_factory.begin() as session:
        workspace, first_client, page, version, actor = _seed(session)
        old_call, chat = _call(session, workspace, first_client, page, version)
        next_client = MCPClient(workspace_id=workspace.id, name="Continuation client")
        session.add(next_client)
        session.flush()
        new_calls = [
            _call(
                session,
                workspace,
                next_client,
                page,
                version,
                chat_token=old_call.mcp_session_id,
            )[0]
            for _ in range(2)
        ]
        assert chat.client_id == first_client.id
        projections = project_mcp_activity_batch(session, [old_call, *new_calls], actor)
        new_key = projections[new_calls[0].id].grouping_key
        assert new_key is not None
        assert projections[new_calls[1].id].grouping_key == new_key
        assert projections[old_call.id].grouping_key != new_key


def test_library_identity_tracks_served_text_and_conversion_generation():
    version = SimpleNamespace(
        id=uuid4(),
        content_hash="a" * 64,
        processing_status="ready",
        selected_conversion_attempt_id=uuid4(),
        selected_preview_attempt_id=None,
    )
    resource_id = uuid4()
    with capture_served_reads() as snapshots:
        record_served_read(
            "library_item", resource_id, version, content="first conversion"
        )
    with capture_served_reads() as changed:
        record_served_read(
            "library_item", resource_id, version, content="second conversion"
        )
    assert snapshots[0]["content_identity"] != changed[0]["content_identity"]
    with capture_served_reads() as metadata:
        record_served_read("library_item", resource_id, version)
    version.selected_conversion_attempt_id = uuid4()
    with capture_served_reads() as newer_metadata:
        record_served_read("library_item", resource_id, version)
    assert metadata[0]["content_identity"] != newer_metadata[0]["content_identity"]


@pytest.mark.parametrize("kind", ["agent_page", "library_item"])
def test_resource_visibility_and_workspace_gate_before_names(session_factory, kind):
    from backend.models.agent import AgentPage, AgentPageVersion
    from backend.models.library import LibraryItem, LibraryItemVersion

    with session_factory.begin() as session:
        workspace, client, context, context_version, actor = _seed(session)
        if kind == "agent_page":
            resource = AgentPage(
                workspace_id=workspace.id,
                category="skill",
                slug=f"private-{uuid4().hex}",
                title="Sensitive resource SECRET",
                lifecycle_status="active",
                sensitivity="workspace",
            )
            session.add(resource)
            session.flush()
            version = AgentPageVersion(
                workspace_id=workspace.id,
                agent_page_id=resource.id,
                version_no=1,
                body_markdown="safe",
                actor_type="human",
                version_status="active",
            )
            session.add(version)
            session.flush()
            resource.current_active_version_id = version.id
        else:
            resource = LibraryItem(
                workspace_id=workspace.id,
                title="Sensitive resource SECRET",
                uri=f"https://example.com/{uuid4()}",
                connector_type="url",
                format_hint="text",
                trust_level="trusted",
                lifecycle_status="ready",
            )
            session.add(resource)
            session.flush()
            version = LibraryItemVersion(
                workspace_id=workspace.id,
                library_item_id=resource.id,
                version_no=1,
                content_hash="a" * 64,
                processing_status="ready",
                raw_storage_ref="test://source",
            )
            session.add(version)
            session.flush()
            resource.current_ready_version_id = resource.current_version_id = version.id
        call, chat = _call(session, workspace, client, context, context_version)
        session.query(McpChatToolCallArtifact).filter(
            McpChatToolCallArtifact.mcp_tool_call_id == call.id
        ).delete(synchronize_session=False)
        McpChatService().link_artifact(
            session=session,
            chat_session=chat,
            tool_call=call,
            resource_kind=McpChatArtifactKind(kind),
            resource_id=resource.id,
            verb=McpChatArtifactVerb.REFERENCED,
        )
        with capture_served_reads() as snapshots:
            record_served_read(kind, resource.id, version)
        call.result_detail = {
            "read_projection": finish_served_read(
                "fetch", {"type": kind, "id": str(resource.id)}, snapshots
            )
        }
        call.tool_name = "fetch"
        call.input_summary = {"type": kind, "id": str(resource.id)}
        session.flush()
        assert project_mcp_activity(session, call, actor).resource_id == resource.id
        if kind == "library_item":
            # A newer ready version does not erase the historical read.
            newer = LibraryItemVersion(
                workspace_id=workspace.id,
                library_item_id=resource.id,
                version_no=2,
                content_hash="b" * 64,
                processing_status="ready",
                raw_storage_ref="test://newer",
            )
            session.add(newer)
            session.flush()
            resource.current_ready_version_id = resource.current_version_id = newer.id
            assert project_mcp_activity(session, call, actor).resource_id == resource.id
        if kind == "agent_page":
            resource.sensitivity = "private"
        else:
            resource.trust_level = "restricted"
        hidden = project_mcp_activity(session, call, actor)
        assert hidden.visible is False
        assert hidden.resource_id is None and hidden.title is None
        assert "SECRET" not in repr(hidden)
        # Workspace-bound edges cannot rescue metadata pointing elsewhere.
        resource.lifecycle_status = "archived"
        assert project_mcp_activity(session, call, actor).visible is False


def test_explicit_other_workspace_version_never_discloses_target(session_factory):
    with session_factory.begin() as session:
        workspace, client, page, version, actor = _seed(session)
        call, _ = _call(session, workspace, client, page, version)
        other_workspace = Workspace(
            slug=f"other-{uuid4().hex}", title="Other", name="Other"
        )
        session.add(other_workspace)
        session.flush()
        other = ContextPage(
            workspace_id=other_workspace.id,
            slug="elsewhere",
            title="Other SECRET",
            page_type="overview",
        )
        session.add(other)
        session.flush()
        other_version = ContextPageVersion(
            workspace_id=other_workspace.id,
            page_id=other.id,
            version_no=1,
            body_md="body",
            content_hash="a" * 64,
            actor_type="human",
            created_by_actor_kind="human",
            created_by_actor_id="seed",
            version_status="published_snapshot",
        )
        session.add(other_version)
        session.flush()
        call.input_summary = {
            "target_type": "context_page_version",
            "target_id": str(other_version.id),
        }
        assert project_mcp_activity(session, call, actor).visible is False


def test_batch_shares_visibility_and_edge_queries_for_repeated_reads(session_factory):
    with session_factory.begin() as session:
        workspace, client, page, version, actor = _seed(session)
        token = uuid4()
        calls = [
            _call(session, workspace, client, page, version, chat_token=token)[0]
            for _ in range(20)
        ]
        statements = []

        def capture(_connection, _cursor, statement, _parameters, _context, _many):
            statements.append(statement)

        connection = session.connection()
        event.listen(connection, "before_cursor_execute", capture)
        try:
            projections = project_mcp_activity_batch(session, calls, actor)
        finally:
            event.remove(connection, "before_cursor_execute", capture)
        assert len(projections) == 20
        assert (
            len({projection.grouping_key for projection in projections.values()}) == 1
        )
        assert all(
            projection.grouping_key is not None for projection in projections.values()
        )
        assert len(statements) < 10

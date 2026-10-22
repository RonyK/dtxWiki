from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from backend.app.main import create_app
from backend.app.settings import Settings
from backend.models.audit import AuditEvent
from backend.models.context import ContextPage, ContextPageVersion
from backend.models.mcp_chat import McpChatSession, McpChatToolCallArtifact
from backend.models.mcp_trace import MCPToolCall
from backend.models.user import User
from backend.models.workspace import MCPClient, Workspace
from backend.policy.auth import ActorContext
from backend.policy.rbac import grant_workspace_membership
from backend.services.activity_feed_service import (
    ActivityFeedService,
    InvalidActivityTokenError,
)
from backend.services.activity_service import ActivityService
from backend.services.mcp_chat_service import mcp_session_trace_uuid
from backend.services.review_service import ReviewService
from backend.tests.conftest import authenticate_test_client_in_workspace

BASE = datetime(2026, 10, 20, 12, tzinfo=UTC)


def _setup(session, workspace=None):
    if workspace is None:
        workspace = session.scalar(select(Workspace).where(Workspace.slug == "default"))
    client = session.scalar(
        select(MCPClient).where(MCPClient.workspace_id == workspace.id)
    )
    if client is None:
        client = MCPClient(workspace_id=workspace.id, name="Target workspace client")
        session.add(client)
        session.flush()
    page = ContextPage(
        workspace_id=workspace.id,
        slug=f"feed-{uuid4()}",
        title="Visible guide",
        page_type="overview",
        lifecycle_status="published",
    )
    session.add(page)
    session.flush()
    version = ContextPageVersion(
        workspace_id=workspace.id,
        page_id=page.id,
        version_no=1,
        body_md="# Visible guide",
        content_hash="a" * 64,
        actor_type="human",
        created_by_actor_kind="human",
        created_by_actor_id="author",
        version_status="published_snapshot",
    )
    session.add(version)
    session.flush()
    page.current_version_id = version.id
    page.current_published_version_id = version.id
    conversation = f"conv_{uuid4().hex}"
    chat = McpChatSession(
        workspace_id=workspace.id, client_id=client.id, mcp_session_id=conversation
    )
    session.add(chat)
    session.flush()
    return workspace, client, page, version, chat


def _call(
    session,
    workspace,
    client,
    page,
    version,
    chat,
    index,
    *,
    tool="read",
    status="succeeded",
    at=None,
    read_projection=True,
):
    call = MCPToolCall(
        workspace_id=workspace.id,
        client_id=client.id,
        mcp_session_id=mcp_session_trace_uuid(chat.mcp_session_id),
        request_id=uuid4(),
        tool_name=tool,
        input_summary={"target_type": "context_page", "target_id": str(page.id)},
        result_status=status,
        result_summary="Private raw summary must not appear token=secret-sentinel",
        result_detail={
            "read_projection": {
                "schema": 1,
                "resource_kind": "context_page",
                "resource_id": str(page.id),
                "version_id": str(version.id),
                "version_status": "published_snapshot",
                "content_identity": "a" * 64,
                "scope_identity": "b" * 64,
                "editing_revision": None,
            }
        }
        if read_projection
        else None,
        started_at=at or BASE - timedelta(seconds=index),
        completed_at=at or BASE - timedelta(seconds=index),
        latency_ms=12,
    )
    session.add(call)
    session.flush()
    session.add(
        McpChatToolCallArtifact(
            workspace_id=workspace.id,
            chat_session_id=chat.id,
            mcp_tool_call_id=call.id,
            resource_kind="context_page",
            resource_id=page.id,
            verb="referenced" if tool == "read" else "edited",
        )
    )
    session.flush()
    return call


def _actor(workspace):
    return ActorContext(
        actor_kind="human",
        actor_id="reader",
        workspace_id=workspace.id,
        role="owner",
        workspace_role="owner",
        token_kind="session",
        scopes=("read",),
    )


def _service():
    return ActivityFeedService(
        ActivityService(ReviewService()), cursor_secret="test-feed-secret"
    )


def test_complete_group_crosses_internal50_batch_and_display_page_without_loss(
    session_factory,
):
    service = _service()
    with session_factory.begin() as session:
        setup = _setup(session)
        workspace, client, page, version, chat = setup
        for i in range(10):
            _call(session, *setup, i, tool="update_context_edit")
        for i in range(10, 55):
            _call(session, *setup, i)
        actor = _actor(workspace)
        first = service.list_feed(
            session,
            workspace.id,
            actor=actor,
            actor_role="owner",
            period="all",
            kind="mcp",
            lane="history",
            limit=10,
        )
        assert len(first.entries) == 10 and first.has_more and first.next_cursor
        second = service.list_feed(
            session,
            workspace.id,
            actor=actor,
            actor_role="owner",
            period="all",
            kind="mcp",
            lane="history",
            limit=10,
            cursor=first.next_cursor,
        )
        assert len(second.entries) == 1 and second.entries[0].event_count == 45
        assert not second.has_more and second.represented_event_count == 45
        group = service.read_group(
            session,
            workspace.id,
            actor=actor,
            actor_role="owner",
            token=second.entries[0].group_token,
        )
        assert group.event_count == 45 and len({e.id for e in group.events}) == 45
        assert all(
            e.target.target_kind == "mcp_tool_call"
            and e.navigation.target_id == page.id
            for e in group.events
        )
        assert "secret-sentinel" not in group.model_dump_json()
        raw = service.list_feed(
            session,
            workspace.id,
            actor=actor,
            actor_role="owner",
            period="all",
            kind="mcp",
            lane="history",
            group_reads=False,
            limit=50,
        )
        raw2 = service.list_feed(
            session,
            workspace.id,
            actor=actor,
            actor_role="owner",
            period="all",
            kind="mcp",
            lane="history",
            group_reads=False,
            limit=50,
            cursor=raw.next_cursor,
        )
        ids = [e.event.id for e in raw.entries + raw2.entries]
        assert len(ids) == len(set(ids)) == 55
        assert session.scalar(select(func.count()).select_from(MCPToolCall)) == 55


def test51_read_limit_and_group_token_revalidates_current_visibility(session_factory):
    service = _service()
    with session_factory.begin() as session:
        setup = _setup(session)
        workspace, _client, page, _version, _chat = setup
        for i in range(51):
            _call(session, *setup, i)
        actor = _actor(workspace)
        grouped = service.list_feed(
            session,
            workspace.id,
            actor=actor,
            actor_role="owner",
            period="all",
            kind="mcp",
            lane="history",
            limit=1,
        )
        assert grouped.entries[0].event_count == 50 and grouped.next_cursor
        tail = service.list_feed(
            session,
            workspace.id,
            actor=actor,
            actor_role="owner",
            period="all",
            kind="mcp",
            lane="history",
            limit=1,
            cursor=grouped.next_cursor,
        )
        assert tail.entries[0].event_count == 1 and tail.next_cursor is None
        token = grouped.entries[0].group_token
        with pytest.raises(InvalidActivityTokenError):
            service.read_group(
                session, uuid4(), actor=actor, actor_role="owner", token=token
            )
        with pytest.raises(InvalidActivityTokenError):
            service.read_group(
                session,
                workspace.id,
                actor=ActorContext(actor_kind="human", actor_id="different"),
                actor_role="owner",
                token=token,
            )
        page.lifecycle_status = "archived"
        session.flush()
        current = service.read_group(
            session, workspace.id, actor=actor, actor_role="owner", token=token
        )
        assert current.event_count == 0 and current.events == []
        hidden = service.list_feed(
            session,
            workspace.id,
            actor=actor,
            actor_role="owner",
            period="all",
            kind="mcp",
        )
        assert hidden.entries == [] and hidden.represented_event_count == 0


def test_completely_private_call_neither_counts_nor_breaks_visible_read_run(
    session_factory,
):
    service = _service()
    with session_factory.begin() as session:
        setup = _setup(session)
        workspace, client, page, version, chat = setup
        first = _call(session, *setup, 0)
        private_page = ContextPage(
            workspace_id=workspace.id,
            slug=f"hidden-{uuid4()}",
            title="Private sentinel",
            page_type="overview",
            lifecycle_status="archived",
        )
        session.add(private_page)
        session.flush()
        _call(session, workspace, client, private_page, version, chat, 1)
        last = _call(session, *setup, 2)
        result = service.list_feed(
            session,
            workspace.id,
            actor=_actor(workspace),
            actor_role="owner",
            period="all",
            kind="mcp",
        )
        assert len(result.entries) == 1 and result.represented_event_count == 2
        group = service.read_group(
            session,
            workspace.id,
            actor=_actor(workspace),
            actor_role="owner",
            token=result.entries[0].group_token,
        )
        assert [event.id for event in group.events] == [first.id, last.id]
        assert "Private sentinel" not in result.model_dump_json()


def test_subtype_precedes_budget_after_boundaries_and_legacy_stays_single(
    session_factory,
):
    service = _service()
    with session_factory.begin() as session:
        setup = _setup(session)
        workspace = setup[0]
        _call(session, *setup, 0)
        _call(session, *setup, 1, tool="search")
        _call(session, *setup, 2)
        _call(session, *setup, 3, read_projection=False)
        result = service.list_feed(
            session,
            workspace.id,
            actor=_actor(workspace),
            actor_role="owner",
            period="all",
            kind="mcp",
            subtype="read",
            lane="history",
            limit=2,
        )
        assert [entry.event_count for entry in result.entries] == [1, 1]
        assert result.has_more and result.next_cursor
        last = service.list_feed(
            session,
            workspace.id,
            actor=_actor(workspace),
            actor_role="owner",
            period="all",
            kind="mcp",
            subtype="read",
            lane="history",
            limit=2,
            cursor=result.next_cursor,
        )
        assert len(last.entries) == 1 and last.entries[0].entry_type == "event"


def test_sparse_filter_scans_a_bounded_window_and_resumes_by_cursor(
    session_factory, monkeypatch
):
    from backend.services import activity_feed_service

    monkeypatch.setattr(activity_feed_service, "_BATCH_SIZE", 2)
    monkeypatch.setattr(activity_feed_service, "_SCAN_BUDGET", 4)
    service = _service()
    with session_factory.begin() as session:
        setup = _setup(session)
        workspace = setup[0]
        for i in range(6):
            _call(session, *setup, i)
        search = _call(session, *setup, 6, tool="search")
        query = dict(
            actor=_actor(workspace),
            actor_role="owner",
            period="all",
            kind="mcp",
            subtype="search",
            lane="history",
        )
        first = service.list_feed(session, workspace.id, **query)
        # The budget ends before any match: an empty page that still carries
        # the scan forward instead of walking the rest of the ledger.
        assert first.entries == [] and first.has_more and first.next_cursor
        second = service.list_feed(
            session, workspace.id, cursor=first.next_cursor, **query
        )
        assert [entry.event.id for entry in second.entries] == [search.id]
        assert not second.has_more and second.next_cursor is None


def test_private_editing_audit_and_mcp_transitions(session_factory):
    service = _service()
    with session_factory.begin() as session:
        setup = _setup(session)
        workspace, _client, page, version, _chat = setup
        for action in (
            "page.begin_context_edit",
            "page.patch_context_edit",
            "page.discard_context_edit",
            "page.publish",
        ):
            session.add(
                AuditEvent(
                    workspace_id=workspace.id,
                    actor_kind="human",
                    actor_id="author",
                    action=action,
                    target_kind="context_page",
                    target_id=page.id,
                    payload={"version_id": str(version.id)},
                    created_at=BASE,
                )
            )
        for index, tool in enumerate(
            (
                "begin_context_edit",
                "discard_context_edit",
                "submit_context_edit_for_review",
            )
        ):
            _call(session, *setup, index, tool=tool, read_projection=False)
        session.flush()

        def feed(actor_id, kind):
            actor = ActorContext(
                actor_kind="human",
                actor_id=actor_id,
                workspace_id=workspace.id,
                role="owner",
                workspace_role="owner",
                token_kind="session",
                scopes=("read",),
            )
            return service.list_feed(
                session,
                workspace.id,
                actor=actor,
                actor_role="owner",
                period="all",
                kind=kind,
            ).entries

        def page_actions(actor_id):
            return {
                entry.event.body
                for entry in feed(actor_id, "workspace")
                if (entry.event.body or "").startswith("page.")
            }

        # An editing session is the author's alone, as its MCP calls are.
        assert page_actions("reader") == {"page.publish"}
        assert page_actions("author") == {
            "page.begin_context_edit",
            "page.patch_context_edit",
            "page.discard_context_edit",
            "page.publish",
        }
        # MCP editing transitions use the audit vocabulary, not a blanket edit.
        assert [entry.event.operation for entry in feed("reader", "mcp")] == [
            "log",
            "delete",
            "review",
        ]


def test_equal_timestamps_total_order_and_new_completion_cannot_shift_cursor(
    session_factory,
):
    service = _service()
    with session_factory.begin() as session:
        setup = _setup(session)
        workspace = setup[0]
        ids = []
        for i in range(60):
            audit = AuditEvent(
                id=UUID(int=i + 1),
                workspace_id=workspace.id,
                actor_kind="human",
                actor_id="reader",
                action="context_page.updated",
                target_kind="context_page",
                target_id=setup[2].id,
                created_at=BASE,
            )
            session.add(audit)
            ids.append(audit.id)
        session.flush()
        actor = _actor(workspace)
        first = service.list_feed(
            session,
            workspace.id,
            actor=actor,
            actor_role="owner",
            period="all",
            lane="history",
            limit=7,
        )
        # A new completion after the signed snapshot must not push old pages.
        _call(session, *setup, 100, at=datetime.now(UTC))
        seen = [e.event.id for e in first.entries]
        cursor = first.next_cursor
        while cursor:
            page = service.list_feed(
                session,
                workspace.id,
                actor=actor,
                actor_role="owner",
                period="all",
                lane="history",
                limit=7,
                cursor=cursor,
            )
            seen.extend(e.event.id for e in page.entries)
            cursor = page.next_cursor
        assert seen == list(reversed(ids))
        assert len(set(seen)) == 60


def test_api_new_contract_cursor_error_and_legacy_raw_are_independent(page_service):
    client = TestClient(
        create_app(
            settings=Settings(
                database_url="sqlite+pysqlite:///:memory:",
                session_secret="test-session-secret",
            ),
            page_service=page_service,
        )
    )
    authenticate_test_client_in_workspace(
        client, page_service.session_factory, workspace_slug="default"
    )
    workspace_id = client.get("/api/v1/workspaces").json()["data"][
        "current_workspace_id"
    ]
    base_url = f"/api/v1/workspaces/{workspace_id}/activity"
    new = client.get(base_url + "/feed?period=all")
    assert new.status_code == 200, new.text
    assert "entries" in new.json()["data"] and "items" not in new.json()["data"]
    old = client.get(base_url + "?period=all")
    assert old.status_code == 200 and "items" in old.json()["data"]
    invalid = client.get(base_url + "/feed?period=all&lane=history&cursor=garbled")
    assert (
        invalid.status_code == 400
        and invalid.json()["error_code"] == "invalid_activity_token"
    )
    invalid_group = client.get(base_url + "/read-group?token=garbled")
    assert invalid_group.status_code == 400
    assert (
        client.get(base_url + "/feed?period=all&kind=system&subtype=read").status_code
        == 400
    )


def test_api_uses_target_workspace_role_and_resolves_identity_after_workspace_read(
    page_service,
):
    client = TestClient(
        create_app(
            settings=Settings(
                database_url="sqlite+pysqlite:///:memory:",
                session_secret="test-session-secret",
            ),
            page_service=page_service,
        )
    )
    authenticate_test_client_in_workspace(
        client, page_service.session_factory, workspace_slug="default"
    )
    with page_service.session_factory.begin() as session:
        user = session.scalar(select(User).where(User.email == "test@example.com"))
        user.display_name = "Resolved reader name"
        target = Workspace(slug="feed-target-role", title="Target", name="Target")
        session.add(target)
        session.flush()
        grant_workspace_membership(
            session, user_id=user.id, workspace_id=target.id, role="viewer"
        )
        setup = _setup(session, target)
        page, version = setup[2], setup[3]
        page.current_published_version_id = None
        page.current_draft_version_id = version.id
        page.lifecycle_status = "draft"
        version.version_status = "draft"
        _call(session, *setup, 0)
        session.add(
            AuditEvent(
                workspace_id=target.id,
                actor_kind="human",
                actor_id=str(user.id),
                action="context_page.updated",
                target_kind="workspace",
                target_id=target.id,
                created_at=BASE,
            )
        )
        target_id, user_id = target.id, user.id
    endpoint = f"/api/v1/workspaces/{target_id}/activity/feed?period=all"
    visible = client.get(endpoint)
    assert visible.status_code == 200, visible.text
    entries = visible.json()["data"]["entries"]
    assert (
        len(entries) == 1
        and entries[0]["event"]["actor"]["display_name"] == "Resolved reader name"
    )
    assert "Visible guide" not in visible.text
    with page_service.session_factory.begin() as session:
        grant_workspace_membership(
            session, user_id=user_id, workspace_id=target_id, role="editor"
        )
    allowed = client.get(endpoint)
    assert allowed.status_code == 200
    assert any(
        entry["event"]["tool_name"] == "read"
        for entry in allowed.json()["data"]["entries"]
    )


def test_same_timestamp_same_uuid_different_sources_have_distinct_cursor_positions(
    session_factory,
):
    service = _service()
    with session_factory.begin() as session:
        setup = _setup(session)
        workspace = setup[0]
        call = _call(session, *setup, 0, at=BASE)
        session.add(
            AuditEvent(
                id=call.id,
                workspace_id=workspace.id,
                actor_kind="human",
                actor_id="reader",
                action="context_page.updated",
                target_kind="context_page",
                target_id=setup[2].id,
                created_at=BASE,
            )
        )
        session.flush()
        first = service.list_feed(
            session,
            workspace.id,
            actor=_actor(workspace),
            actor_role="owner",
            period="all",
            lane="history",
            limit=1,
        )
        assert first.entries[0].event.tool_name == "read" and first.next_cursor
        second = service.list_feed(
            session,
            workspace.id,
            actor=_actor(workspace),
            actor_role="owner",
            period="all",
            lane="history",
            limit=1,
            cursor=first.next_cursor,
        )
        assert second.entries[0].event.tool_name is None
        assert first.entries[0].event.id == second.entries[0].event.id
        assert (
            first.entries[0].id != second.entries[0].id and second.next_cursor is None
        )


def test_call_started_before_snapshot_but_completed_after_is_excluded_on_later_page(
    session_factory,
):
    service = _service()
    with session_factory.begin() as session:
        setup = _setup(session)
        workspace = setup[0]
        for i in range(3):
            _call(session, *setup, i, tool="update_context_edit")
        actor = _actor(workspace)
        first = service.list_feed(
            session,
            workspace.id,
            actor=actor,
            actor_role="owner",
            period="all",
            lane="history",
            limit=1,
        )
        later = _call(session, *setup, 100, tool="update_context_edit")
        later.started_at = BASE - timedelta(hours=1)
        later.completed_at = datetime.now(UTC)
        session.flush()
        second = service.list_feed(
            session,
            workspace.id,
            actor=actor,
            actor_role="owner",
            period="all",
            lane="history",
            limit=50,
            cursor=first.next_cursor,
        )
        assert len(second.entries) == 2 and later.id not in {
            e.event.id for e in second.entries
        }


def test_overview_reserves_visible_review_and_unread_is_raw_attention_not_entry_budget(
    page_service,
):
    from backend.models.job import Job
    from backend.models.review import ReviewItem

    client = TestClient(
        create_app(
            settings=Settings(
                database_url="sqlite+pysqlite:///:memory:",
                session_secret="test-session-secret",
            ),
            page_service=page_service,
        )
    )
    authenticate_test_client_in_workspace(
        client, page_service.session_factory, workspace_slug="default"
    )
    workspace_id = client.get("/api/v1/workspaces").json()["data"][
        "current_workspace_id"
    ]
    pages = f"/api/v1/workspaces/{workspace_id}/context/pages"
    created = client.post(
        pages, json={"title": "Reserved review", "body_md": "# Reserved review"}
    )
    assert created.status_code == 201, created.text
    page = created.json()["data"]
    submitted = client.post(
        f"{pages}/{page['id']}/submit-for-review",
        json={
            "idempotency_key": f"submit-{page['id']}",
            "version_id": page["version_id"],
            "expected_editing_revision": page["editing_revision"],
        },
    )
    assert submitted.status_code == 200, submitted.text
    with page_service.session_factory.begin() as session:
        review = session.scalar(
            select(ReviewItem).where(ReviewItem.workspace_id == UUID(workspace_id))
        )
        review.created_at = BASE - timedelta(days=1)
        review_id = str(review.id)
        session.add_all(
            [
                Job(
                    workspace_id=UUID(workspace_id),
                    job_type="library_extraction",
                    trigger_type="manual",
                    status="failed",
                    updated_at=BASE + timedelta(seconds=i),
                    last_error="/private/path token=secret-sentinel",
                )
                for i in range(5)
            ]
        )
        session.add(
            Job(
                workspace_id=UUID(workspace_id),
                job_type="embed",
                trigger_type="manual",
                status="failed",
                updated_at=BASE,
            )
        )
    endpoint = f"/api/v1/workspaces/{workspace_id}/activity/feed?period=all&limit=1"
    grouped = client.get(endpoint)
    assert grouped.status_code == 200, grouped.text
    data = grouped.json()["data"]
    assert data["entries"][0]["event"]["review_item_id"] == review_id
    assert (
        data["entries"][0]["event"]["navigation"]["href"]
        == f"/review?review={review_id}"
    )
    assert (
        data["unread_count"] == 6
        and data["returned_entry_count"] == data["represented_event_count"] == 1
    )
    assert data["has_more"] and data["next_cursor"] is None
    raw = client.get(endpoint + "&group_reads=false").json()["data"]
    assert raw["unread_count"] == data["unread_count"]
    jobs = client.get(endpoint + "&kind=system&subtype=jobs").json()["data"]
    assert jobs["unread_count"] == 5
    assert jobs["entries"][0]["event"]["result_status"] == "failed"
    assert "secret-sentinel" not in str(jobs)


@pytest.mark.parametrize("audit_scopes", [["mcp:write"], ["mcp:read", "mcp:write"]])
def test_bearer_activity_feed_and_group_require_audit_read(page_service, audit_scopes):
    client = TestClient(
        create_app(
            settings=Settings(
                database_url="sqlite+pysqlite:///:memory:",
                session_secret="test-session-secret",
            ),
            page_service=page_service,
        )
    )
    workspace_id = authenticate_test_client_in_workspace(
        client, page_service.session_factory, workspace_slug="default"
    )
    with page_service.session_factory.begin() as session:
        setup = _setup(session)
        _call(session, *setup, 0)
        _call(session, *setup, 1)
    tokens = []
    for name, scopes in [
        ("Audit feed access", audit_scopes),
        ("Plain read access", ["mcp:read"]),
    ]:
        issued = client.post(
            "/api/v1/auth/tokens",
            json={
                "name": name,
                "scopes": scopes,
                "workspace_grants": [{"workspace_id": workspace_id}],
            },
        )
        assert issued.status_code == 201, issued.text
        tokens.append(issued.json()["data"]["plain_token"])
    base = f"/api/v1/workspaces/{workspace_id}/activity"
    allowed = client.get(
        base + "/feed?period=all&kind=mcp",
        headers={"Authorization": f"Bearer {tokens[0]}"},
    )
    assert allowed.status_code == 200, allowed.text
    group = next(
        entry
        for entry in allowed.json()["data"]["entries"]
        if entry["entry_type"] == "read_group"
    )
    details = client.get(
        base + "/read-group",
        params={"token": group["group_token"]},
        headers={"Authorization": f"Bearer {tokens[0]}"},
    )
    assert details.status_code == 200 and details.json()["data"]["event_count"] == 2
    # An authenticated browser cookie remains present, proving the bearer scope
    # guard wins rather than silently falling back to the cookie's full rights.
    denied = client.get(
        base + "/feed?period=all", headers={"Authorization": f"Bearer {tokens[1]}"}
    )
    assert denied.status_code == 403 and denied.json()["error_code"] == "scope_denied"
    denied_group = client.get(
        base + "/read-group",
        params={"token": group["group_token"]},
        headers={"Authorization": f"Bearer {tokens[1]}"},
    )
    assert (
        denied_group.status_code == 403
        and denied_group.json()["error_code"] == "scope_denied"
    )


def test_review_reservation_uses_closed_snapshot_and_exclusive_period_bound(
    session_factory, monkeypatch
):
    from backend.models.change import ChangeSet
    from backend.models.review import ReviewItem
    from backend.services import activity_feed_service

    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return BASE if tz else BASE.replace(tzinfo=None)

    monkeypatch.setattr(activity_feed_service, "datetime", FrozenDateTime)
    service = _service()
    with session_factory.begin() as session:
        workspace = session.scalar(select(Workspace).where(Workspace.slug == "default"))
        change = ChangeSet(
            workspace_id=workspace.id, trigger_type="manual", actor_type="human"
        )
        session.add(change)
        session.flush()
        reviews = []
        for difference in [-1, 0, 1]:
            review = ReviewItem(
                workspace_id=workspace.id,
                change_set_id=change.id,
                review_type="context_page_publish",
                created_at=BASE + timedelta(microseconds=difference),
            )
            session.add(review)
            reviews.append(review)
        session.flush()
        query = dict(
            session=session,
            workspace_id=workspace.id,
            since=None,
            until=None,
            snapshot=BASE,
            limit=50,
            audience_for=None,
            actor_role="owner",
        )
        reserved = service.activity._pending_reviews(**query)
        assert [review.id for review in reserved] == [reviews[1].id, reviews[0].id]
        # Period ends remain exclusive independently of the inclusive event
        # snapshot, so the first instant of the next UTC period never leaks in.
        query["until"] = BASE
        assert [review.id for review in service.activity._pending_reviews(**query)] == [
            reviews[0].id
        ]
        feed = service.list_feed(
            session,
            workspace.id,
            actor=_actor(workspace),
            actor_role="owner",
            period="all",
            limit=1,
        )
        assert feed.entries[0].event.review_item_id == reviews[1].id
        assert feed.unread_count == 2 and feed.has_more


def test_merged_feed_and_group_hydration_isolate_every_source_from_foreign_workspace(
    session_factory,
):
    from backend.models.change import ChangeSet
    from backend.models.job import Job
    from backend.models.review import ReviewItem

    service = _service()
    with session_factory.begin() as session:
        own = _setup(session)
        foreign_workspace = Workspace(
            slug="feed-foreign-inventory",
            title="Foreign workspace",
            name="Foreign workspace",
        )
        session.add(foreign_workspace)
        session.flush()
        foreign = _setup(session, foreign_workspace)
        for setup, label in [(own, "Home"), (foreign, "FOREIGN-SENTINEL")]:
            workspace = setup[0]
            _call(session, *setup, 0)
            _call(session, *setup, 1)
            change = ChangeSet(
                workspace_id=workspace.id, trigger_type="manual", actor_type="human"
            )
            session.add(change)
            session.flush()
            session.add(
                ReviewItem(
                    workspace_id=workspace.id,
                    change_set_id=change.id,
                    review_type="context_page_publish",
                    created_at=BASE - timedelta(seconds=3),
                    decision_summary=label,
                )
            )
            session.add(
                Job(
                    workspace_id=workspace.id,
                    job_type="library_extraction",
                    trigger_type="manual",
                    status="failed",
                    updated_at=BASE - timedelta(seconds=4),
                )
            )
            session.add(
                AuditEvent(
                    workspace_id=workspace.id,
                    actor_kind="human",
                    actor_id="reader",
                    action="context_page.updated",
                    target_kind="context_page",
                    target_id=setup[2].id,
                    event_summary=label,
                    created_at=BASE - timedelta(seconds=5),
                )
            )
        # Global audit rows share the ledger, but never the workspace feed.
        session.add(
            AuditEvent(
                workspace_id=None,
                actor_kind="human",
                actor_id="reader",
                action="auth.session.created",
                target_kind="workspace",
                target_id=foreign_workspace.id,
                event_summary="GLOBAL-SENTINEL",
                created_at=BASE,
            )
        )
        session.flush()
        actor = _actor(own[0])
        result = service.list_feed(
            session, own[0].id, actor=actor, actor_role="owner", period="all"
        )
        assert result.returned_entry_count == 4 and result.represented_event_count == 5
        assert result.unread_count == 2
        assert "FOREIGN-SENTINEL" not in result.model_dump_json()
        assert "GLOBAL-SENTINEL" not in result.model_dump_json()
        assert str(foreign[2].id) not in result.model_dump_json()
        group = next(
            entry for entry in result.entries if entry.entry_type == "read_group"
        )
        members = service.read_group(
            session, own[0].id, actor=actor, actor_role="owner", token=group.group_token
        )
        assert members.event_count == 2
        assert all(event.navigation.target_id == own[2].id for event in members.events)

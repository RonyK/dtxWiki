from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from sqlalchemy import select

from backend.app.main import create_app
from backend.app.settings import Settings
from backend.models.context import ContextPage, ContextPageVersion
from backend.models.mcp_chat import McpChatArtifactKind, McpChatArtifactVerb
from backend.models.mcp_trace import MCPToolCall
from backend.models.user import User
from backend.models.workspace import MCPClient, Workspace
from backend.policy.rbac import grant_workspace_membership
from backend.services.mcp_chat_service import McpChatService
from backend.tests.conftest import authenticate_test_client_in_workspace


def _publish_page(session, page):
    # A linked context page must carry a visible published version, else the
    # actor-gated mcp-chat read redacts it (a real linked page always has one).
    version = ContextPageVersion(
        workspace_id=page.workspace_id,
        page_id=page.id,
        version_no=1,
        body_md=f"# {page.title}\n\nbody",
        content_hash=f"hash-{page.slug}",
        actor_type="human",
        created_by_actor_kind="human",
        created_by_actor_id="seed",
        version_status="published_snapshot",
    )
    session.add(version)
    session.flush()
    page.current_published_version_id = version.id
    session.flush()


def make_client(page_service, email: str = "test@example.com") -> TestClient:
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
        client,
        page_service.session_factory,
        email=email,
    )
    return client


def set_role(page_service, email: str, workspace_id: str, role: str) -> None:
    with page_service.session_factory.begin() as session:
        user = session.scalar(select(User).where(User.email == email))
        workspace = session.get(Workspace, UUID(workspace_id))
        assert user is not None
        assert workspace is not None
        grant_workspace_membership(
            session,
            user_id=user.id,
            workspace_id=workspace.id,
            role=role,
        )


def test_mcp_chats_api_lists_reads_and_archives_chat(page_service):
    client = make_client(page_service)
    workspace_id = client.get("/api/v1/workspaces").json()["data"][
        "current_workspace_id"
    ]
    chat_id, page_id = _seed_chat(page_service, UUID(workspace_id))

    listing = client.get(f"/api/v1/workspaces/{workspace_id}/mcp-chats")
    assert listing.status_code == 200
    listed_chat = listing.json()["data"]["items"][0]
    assert listed_chat["id"] == str(chat_id)
    assert (
        listed_chat["title_source"],
        listed_chat["action"],
        listed_chat["subject"],
    ) == ("artifact", "create", "MCP API Page")

    detail = client.get(f"/api/v1/workspaces/{workspace_id}/mcp-chats/{chat_id}")
    assert detail.status_code == 200
    assert detail.json()["data"]["linked_artifacts"][0]["resource_id"] == str(page_id)
    assert detail.json()["data"]["title_source"] == "artifact"

    provenance = client.get(
        f"/api/v1/workspaces/{workspace_id}/mcp-chats/resource-provenance"
        f"?kind=context_page&id={page_id}"
    )
    assert provenance.status_code == 200
    assert provenance.json()["data"]["items"][0]["chat_session"]["id"] == str(chat_id)

    updated = client.patch(
        f"/api/v1/workspaces/{workspace_id}/mcp-chats/{chat_id}",
        json={"title": "User titled chat"},
    )
    assert updated.status_code == 200
    assert updated.json()["data"]["title"] == "User titled chat"
    assert updated.json()["data"]["title_source"] == "user"
    assert updated.json()["data"]["subject"] == "User titled chat"

    archived = client.delete(f"/api/v1/workspaces/{workspace_id}/mcp-chats/{chat_id}")
    assert archived.status_code == 200
    assert archived.json()["data"]["chat_session_id"] == str(chat_id)


def test_mcp_chats_api_minimizes_list_and_provenance_client_identity(page_service):
    client = make_client(page_service)
    workspace_id = client.get("/api/v1/workspaces").json()["data"][
        "current_workspace_id"
    ]
    raw_name = (
        "https://client.example/callback?code=URL_SECRET#fragment "
        "클라이언트 👩🏽‍💻 access_token=EARLY_SECRET "
        "redirect_uri=https://redirect.example/private?token=REDIRECT_SECRET "
        + ("가" * 100)
        + "\u202e"
    )
    expected_scopes = ["mcp:read", "mcp:write", "admin:secret"]
    with page_service.session_factory.begin() as session:
        mcp_client = session.scalar(
            select(MCPClient).where(MCPClient.workspace_id == UUID(workspace_id))
        )
        mcp_client.name = raw_name
        mcp_client.scopes = expected_scopes

    chat_id, page_id = _seed_chat(page_service, UUID(workspace_id))
    listing = client.get(f"/api/v1/workspaces/{workspace_id}/mcp-chats")
    detail = client.get(f"/api/v1/workspaces/{workspace_id}/mcp-chats/{chat_id}")
    provenance = client.get(
        f"/api/v1/workspaces/{workspace_id}/mcp-chats/resource-provenance"
        f"?kind=context_page&id={page_id}"
    )

    assert listing.status_code == 200
    assert detail.status_code == 200
    assert provenance.status_code == 200
    list_payload = listing.json()["data"]
    list_client = list_payload["items"][0]["client"]
    filter_client = list_payload["clients"][0]
    provenance_client = provenance.json()["data"]["items"][0]["chat_session"]["client"]
    detail_client = detail.json()["data"]["client"]

    for projected in (list_client, filter_client, provenance_client):
        assert projected["scopes"] == []
        assert projected["name"].endswith("…")
        assert "클라이언트" in projected["name"]
        assert "👩🏽‍💻" in projected["name"]
        assert "\u202e" not in projected["name"]
        for secret in (
            "URL_SECRET",
            "fragment",
            "EARLY_SECRET",
            "REDIRECT_SECRET",
            "access_token",
            "redirect_uri",
        ):
            assert secret not in projected["name"]

    assert detail_client["scopes"] == expected_scopes
    assert len(detail_client["name"]) > len(list_client["name"])
    assert "클라이언트" in detail_client["name"]
    assert "👩🏽‍💻" in detail_client["name"]
    assert "\u202e" not in detail_client["name"]
    for secret in (
        "URL_SECRET",
        "fragment",
        "EARLY_SECRET",
        "REDIRECT_SECRET",
        "access_token",
        "redirect_uri",
    ):
        assert secret not in detail_client["name"]


def test_mcp_chats_api_archive_then_unarchive_clears_archived_at(page_service):
    client = make_client(page_service)
    workspace_id = client.get("/api/v1/workspaces").json()["data"][
        "current_workspace_id"
    ]
    chat_id, _page_id = _seed_chat(page_service, UUID(workspace_id))

    archived = client.delete(f"/api/v1/workspaces/{workspace_id}/mcp-chats/{chat_id}")
    assert archived.status_code == 200
    assert archived.json()["data"]["archived_at"] is not None

    unarchived = client.post(
        f"/api/v1/workspaces/{workspace_id}/mcp-chats/{chat_id}/unarchive"
    )
    assert unarchived.status_code == 200
    body = unarchived.json()["data"]
    assert body["chat_session_id"] == str(chat_id)
    # Unarchive clears the timestamp so the session leaves the Archived filter.
    assert body["archived_at"] is None
    assert body["audit_event_id"] is not None

    # The chat is back in the default (non-archived) listing.
    listing = client.get(f"/api/v1/workspaces/{workspace_id}/mcp-chats")
    assert [item["id"] for item in listing.json()["data"]["items"]] == [str(chat_id)]


def test_mcp_chats_api_unarchive_rejects_viewer_and_allows_editor(page_service):
    owner = make_client(page_service)
    member = make_client(page_service, "mcp-chats-unarchive@example.com")
    workspace_id = owner.get("/api/v1/workspaces").json()["data"][
        "current_workspace_id"
    ]
    chat_id, _page_id = _seed_chat(page_service, UUID(workspace_id))
    owner.delete(f"/api/v1/workspaces/{workspace_id}/mcp-chats/{chat_id}")
    path = f"/api/v1/workspaces/{workspace_id}/mcp-chats/{chat_id}/unarchive"

    set_role(page_service, "mcp-chats-unarchive@example.com", workspace_id, "viewer")
    rejected = member.post(path)
    assert rejected.status_code == 403
    assert rejected.json()["error_code"] == "workspace_role_insufficient"

    set_role(page_service, "mcp-chats-unarchive@example.com", workspace_id, "editor")
    allowed = member.post(path)
    assert allowed.status_code == 200
    assert allowed.json()["data"]["archived_at"] is None


def test_mcp_chats_api_read_endpoints_reject_viewer_and_allow_editor(page_service):
    owner = make_client(page_service)
    member = make_client(page_service, "mcp-chats-member@example.com")
    workspace_id = owner.get("/api/v1/workspaces").json()["data"][
        "current_workspace_id"
    ]
    _, page_id = _seed_chat(page_service, UUID(workspace_id))
    paths = [
        f"/api/v1/workspaces/{workspace_id}/mcp-chats",
        f"/api/v1/workspaces/{workspace_id}/mcp-chats/export",
        (
            f"/api/v1/workspaces/{workspace_id}/mcp-chats/resource-provenance"
            f"?kind=context_page&id={page_id}"
        ),
        f"/api/v1/workspaces/{workspace_id}/mcp-chats/resource-links",
    ]

    set_role(page_service, "mcp-chats-member@example.com", workspace_id, "viewer")
    for path in paths:
        rejected = member.get(path)
        assert rejected.status_code == 403, path
        body = rejected.json()
        assert body["error_code"] == "workspace_role_insufficient", path
        assert "envelope" in body, path

    set_role(page_service, "mcp-chats-member@example.com", workspace_id, "editor")
    for path in paths:
        allowed = member.get(path)
        assert allowed.status_code == 200, path
        assert "data" in allowed.json(), path


def test_mcp_chats_api_timeline_links_every_call_that_edited_a_page(page_service):
    """T9b-FU1 at the API layer: a chat where two calls edit the SAME page must
    return BOTH timeline rows carrying the artifact + "Edited: <title>" headline
    and schema_version (not just the latest call)."""
    client = make_client(page_service)
    workspace_id = client.get("/api/v1/workspaces").json()["data"][
        "current_workspace_id"
    ]
    chat_id, page_id = _seed_multi_edit_chat(page_service, UUID(workspace_id))

    detail = client.get(f"/api/v1/workspaces/{workspace_id}/mcp-chats/{chat_id}")
    assert detail.status_code == 200
    timeline = detail.json()["data"]["timeline"]
    assert len(timeline) == 2
    for row in timeline:
        assert [a["resource_id"] for a in row["linked_artifacts"]] == [str(page_id)]
        assert row["linked_artifacts"][0]["verb"] == "edited"
        assert row["headline"] == "Edited: MCP Multi Edit Page"
        assert row["schema_version"] == "llmwiki.mcp.v1"


def _seed_multi_edit_chat(page_service, workspace_id: UUID):
    service = McpChatService()
    with page_service.session_factory.begin() as session:
        workspace = session.scalar(
            select(Workspace).where(Workspace.id == workspace_id)
        )
        client = session.scalar(
            select(MCPClient).where(MCPClient.workspace_id == workspace.id)
        )
        page = ContextPage(
            workspace_id=workspace_id,
            slug="mcp-api-multi-edit",
            title="MCP Multi Edit Page",
            page_type="overview",
        )
        session.add(page)
        session.flush()
        _publish_page(session, page)
        session_uuid = uuid4()
        calls = []
        for _ in range(2):
            call = MCPToolCall(
                client_id=client.id,
                workspace_id=workspace_id,
                mcp_session_id=session_uuid,
                request_id=uuid4(),
                tool_name="update_draft",
                input_summary={"title": "MCP Multi Edit Page"},
                result_status="succeeded",
                result_summary="Tool call succeeded.",
                warnings=[],
                provider="unknown",
                model_id="unknown",
                params_summary={},
                client_self_reported=False,
            )
            session.add(call)
            calls.append(call)
        session.flush()
        chat = service.upsert_session_from_tool_call(
            session=session,
            workspace_id=workspace_id,
            client_id=client.id,
            mcp_session_id=str(session_uuid),
            tool_call=calls[0],
        )
        service.upsert_session_from_tool_call(
            session=session,
            workspace_id=workspace_id,
            client_id=client.id,
            mcp_session_id=str(session_uuid),
            tool_call=calls[1],
        )
        for call in calls:
            service.link_artifact(
                session=session,
                chat_session=chat,
                tool_call=call,
                resource_kind=McpChatArtifactKind.CONTEXT_PAGE,
                resource_id=page.id,
                verb=McpChatArtifactVerb.EDITED,
            )
        session.flush()
        return chat.id, page.id


def _seed_chat(page_service, workspace_id: UUID):
    service = McpChatService()
    with page_service.session_factory.begin() as session:
        workspace = session.scalar(
            select(Workspace).where(Workspace.id == workspace_id)
        )
        client = session.scalar(
            select(MCPClient).where(MCPClient.workspace_id == workspace.id)
        )
        page = ContextPage(
            workspace_id=workspace_id,
            slug="mcp-api-page",
            title="MCP API Page",
            page_type="overview",
        )
        session.add(page)
        session.flush()
        _publish_page(session, page)
        tool_call = MCPToolCall(
            client_id=client.id,
            workspace_id=workspace_id,
            mcp_session_id=uuid4(),
            request_id=uuid4(),
            tool_name="write_draft",
            input_summary={"title": "MCP API Page"},
            result_status="succeeded",
            result_summary="Tool call succeeded.",
            warnings=[],
            provider="unknown",
            model_id="unknown",
            params_summary={},
            client_self_reported=False,
        )
        session.add(tool_call)
        session.flush()
        chat = service.upsert_session_from_tool_call(
            session=session,
            workspace_id=workspace_id,
            client_id=client.id,
            mcp_session_id=str(tool_call.mcp_session_id),
            tool_call=tool_call,
        )
        service.link_artifact(
            session=session,
            chat_session=chat,
            tool_call=tool_call,
            resource_kind=McpChatArtifactKind.CONTEXT_PAGE,
            resource_id=page.id,
            verb=McpChatArtifactVerb.CREATED,
        )
        session.flush()
        return chat.id, page.id


def _seed_preview_chat(page_service, workspace_id: UUID):
    from backend.services.mcp_trace_redaction import redact_tool_arguments

    service = McpChatService()
    with page_service.session_factory.begin() as session:
        client = session.scalar(
            select(MCPClient).where(MCPClient.workspace_id == workspace_id)
        )
        page = ContextPage(
            workspace_id=workspace_id,
            slug="mcp-api-preview-page",
            title="MCP Preview Page",
            page_type="overview",
        )
        session.add(page)
        session.flush()
        _publish_page(session, page)
        tool_call = MCPToolCall(
            client_id=client.id,
            workspace_id=workspace_id,
            mcp_session_id=uuid4(),
            request_id=uuid4(),
            tool_name="patch_context_edit",
            input_summary=redact_tool_arguments(
                "patch_context_edit",
                {
                    "page_id": str(page.id),
                    "version_id": str(page.current_published_version_id),
                    "operations": [
                        {
                            "op": "replace_text",
                            "old_text": "API-PREVIEW-OLD",
                            "new_text": "API-PREVIEW-NEW",
                        }
                    ],
                },
            ),
            result_status="succeeded",
            result_summary="Tool call succeeded.",
            warnings=[],
            provider="unknown",
            model_id="unknown",
            params_summary={},
            client_self_reported=False,
        )
        session.add(tool_call)
        session.flush()
        chat = service.upsert_session_from_tool_call(
            session=session,
            workspace_id=workspace_id,
            client_id=client.id,
            mcp_session_id=str(tool_call.mcp_session_id),
            tool_call=tool_call,
        )
        service.link_artifact(
            session=session,
            chat_session=chat,
            tool_call=tool_call,
            resource_kind=McpChatArtifactKind.CONTEXT_PAGE,
            resource_id=page.id,
            verb=McpChatArtifactVerb.EDITED,
        )
        session.flush()
        return chat.id, tool_call.id


def test_mcp_chats_api_owner_deletes_a_chats_previews(page_service):
    owner = make_client(page_service)
    member = make_client(page_service, "mcp-chats-previews@example.com")
    workspace_id = owner.get("/api/v1/workspaces").json()["data"][
        "current_workspace_id"
    ]
    chat_id, call_id = _seed_preview_chat(page_service, UUID(workspace_id))
    path = f"/api/v1/workspaces/{workspace_id}/mcp-chats/{chat_id}/previews"

    before = owner.get(f"/api/v1/workspaces/{workspace_id}/mcp-chats/{chat_id}")
    assert "API-PREVIEW-NEW" in before.text

    set_role(page_service, "mcp-chats-previews@example.com", workspace_id, "editor")
    rejected = member.delete(path)
    assert rejected.status_code == 403
    assert rejected.json()["error_code"] == "workspace_role_insufficient"

    purged = owner.delete(path)
    assert purged.status_code == 200, purged.text
    body = purged.json()["data"]
    assert body["chat_session_id"] == str(chat_id)
    assert body["purged_call_count"] == 1
    assert body["audit_event_id"]

    after = owner.get(f"/api/v1/workspaces/{workspace_id}/mcp-chats/{chat_id}")
    assert "API-PREVIEW" not in after.text
    call = next(
        entry
        for entry in after.json()["data"]["timeline"]
        if entry["id"] == str(call_id)
    )
    assert call["edit_summary"]["previews_purged"] is True
    assert call["input_summary"]["operations"][0]["old_text_preview"] is None
    with page_service.session_factory() as session:
        stored = session.get(MCPToolCall, call_id)
        assert stored.input_summary["operations"][0]["new_text_preview"] is None


def test_mcp_chats_preview_purge_is_browser_session_only():
    from types import SimpleNamespace

    import pytest
    from fastapi import HTTPException
    from starlette.requests import Request

    from backend.policy import rest_scopes
    from backend.policy.auth import ActorContext

    route_path = (
        "/api/v1/workspaces/{workspace_id}/mcp-chats/{chat_session_id}/previews"
    )
    workspace_id = uuid4()
    chat_id = uuid4()
    request = Request(
        {
            "type": "http",
            "method": "DELETE",
            "path": f"/api/v1/workspaces/{workspace_id}/mcp-chats/{chat_id}/previews",
            "headers": [],
            "path_params": {
                "workspace_id": str(workspace_id),
                "chat_session_id": str(chat_id),
            },
            "route": SimpleNamespace(path=route_path),
        }
    )
    actor = ActorContext(
        actor_kind="human",
        actor_id="broad-pat",
        scopes=("workspace-admin", "draft-write", "audit-read", "mcp:write"),
        token_kind="api_token",
        provenance="personal_access_token",
    )

    with pytest.raises(HTTPException) as exc_info:
        rest_scopes.require_token_scope(request, actor)

    assert exc_info.value.status_code == 403
    assert exc_info.value.detail == {
        "error_code": "scope_denied",
        "message": f"REST endpoint requires a user session: DELETE {route_path}",
    }

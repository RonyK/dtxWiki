"""Conversation continuity is an explicit contract, not a silent default.

A client that omitted `conversation_token` used to get a freshly minted one on
every single call, so one ongoing chat landed in the Chats screen as a row per
tool call (nine rows for one document's work, in the reported case). Standard
MCP clients do not echo a dtxWiki-specific envelope field, and the transport
session id is not a substitute -- it changes at every ephemeral exec, reconnect
and re-registration, which is exactly where the observed splits happened.

So content tools now have to name their conversation. These tests pin that.
"""

import json

import anyio
import pytest
from mcp.shared.memory import create_connected_server_and_client_session
from sqlalchemy import func, select

from backend.app.settings import Settings
from backend.mcp.protocol.server import create_mcp_server
from backend.mcp.scopes import CONTENT_TOOLS
from backend.models.mcp_chat import McpChatSession
from backend.policy.auth import ActorContext


@pytest.fixture
def conversation_actor(mcp_default_actor) -> ActorContext:
    return mcp_default_actor(
        actor_id="conversation-contract-test",
        scopes=("mcp:read", "mcp:write"),
    )


def _server(page_service, actor: ActorContext):
    return create_mcp_server(
        settings=Settings(database_url="sqlite+pysqlite:///:memory:"),
        actor=actor,
        page_service=page_service,
    )


def _chat_count(page_service, workspace_id) -> int:
    with page_service.session_factory() as session:
        return (
            session.scalar(
                select(func.count(McpChatSession.id)).where(
                    McpChatSession.workspace_id == workspace_id
                )
            )
            or 0
        )


def _create_context_page_args(workspace_id, slug: str, **extra):
    return {
        "workspace_id": str(workspace_id),
        "title": slug.replace("-", " ").title(),
        "page_type": "overview",
        "body_md": f"# {slug}\n\nBody.\n",
        **extra,
    }


def test_content_call_without_a_token_is_refused_and_opens_no_chat(
    page_service, conversation_actor
):
    workspace_id = conversation_actor.workspace_id
    assert workspace_id is not None
    before = _chat_count(page_service, workspace_id)

    async def run():
        async with create_connected_server_and_client_session(
            _server(page_service, conversation_actor)
        ) as client:
            return await client.call_tool(
                "create_context_page",
                _create_context_page_args(workspace_id, "no-token"),
            )

    result = anyio.run(run)
    payload = json.loads(result.content[0].text)

    assert result.isError is True
    assert payload["envelope"]["error_code"] == "conversation_token_required"
    # The call never ran, so it must not leave a chat behind -- creating one per
    # rejected call would just relocate the churn.
    assert _chat_count(page_service, workspace_id) == before


def test_new_sentinel_starts_exactly_one_chat_and_the_token_continues_it(
    page_service,
    conversation_actor,
):
    workspace_id = conversation_actor.workspace_id
    assert workspace_id is not None
    before = _chat_count(page_service, workspace_id)

    async def run():
        async with create_connected_server_and_client_session(
            _server(page_service, conversation_actor)
        ) as client:
            first = await client.call_tool(
                "create_context_page",
                _create_context_page_args(
                    workspace_id, "continuity-one", conversation_token="new"
                ),
            )
            token = json.loads(first.content[0].text)["envelope"]["conversation_token"]
            second = await client.call_tool(
                "create_context_page",
                _create_context_page_args(
                    workspace_id, "continuity-two", conversation_token=token
                ),
            )
            third = await client.call_tool(
                "create_context_page",
                _create_context_page_args(
                    workspace_id, "continuity-three", conversation_token=token
                ),
            )
            return first, second, third, token

    first, second, third, token = anyio.run(run)
    for result in (first, second, third):
        payload = json.loads(result.content[0].text)
        assert payload["envelope"]["result_status"] == "succeeded", payload
        assert payload["envelope"]["conversation_token"] == token

    # Three content calls, one chat -- the whole point of the contract.
    assert _chat_count(page_service, workspace_id) == before + 1


def test_meta_carries_the_token_when_the_client_cannot_add_an_argument(
    page_service,
    conversation_actor,
):
    workspace_id = conversation_actor.workspace_id
    assert workspace_id is not None
    before = _chat_count(page_service, workspace_id)

    async def run():
        async with create_connected_server_and_client_session(
            _server(page_service, conversation_actor)
        ) as client:
            first = await client.call_tool(
                "create_context_page",
                _create_context_page_args(
                    workspace_id, "meta-one", _meta={"conversation_token": "new"}
                ),
            )
            token = json.loads(first.content[0].text)["envelope"]["conversation_token"]
            second = await client.call_tool(
                "create_context_page",
                _create_context_page_args(
                    workspace_id, "meta-two", _meta={"conversation_token": token}
                ),
            )
            return first, second, token

    first, second, token = anyio.run(run)
    for result in (first, second):
        payload = json.loads(result.content[0].text)
        assert payload["envelope"]["result_status"] == "succeeded", payload
        assert payload["envelope"]["conversation_token"] == token
    assert _chat_count(page_service, workspace_id) == before + 1


def test_discovery_calls_need_no_token(page_service, conversation_actor):
    workspace_id = conversation_actor.workspace_id
    assert workspace_id is not None

    async def run():
        async with create_connected_server_and_client_session(
            _server(page_service, conversation_actor)
        ) as client:
            return await client.call_tool("guide", {"workspace_id": str(workspace_id)})

    result = anyio.run(run)
    payload = json.loads(result.content[0].text)
    assert result.isError is False
    assert payload["envelope"]["result_status"] == "succeeded", payload
    assert [warning["code"] for warning in payload["envelope"].get("warnings", [])] == [
        "conversation_token_not_supplied"
    ]


def test_discovery_call_with_a_token_stays_in_one_chat_without_warning(
    page_service, conversation_actor
):
    workspace_id = conversation_actor.workspace_id
    assert workspace_id is not None
    before = _chat_count(page_service, workspace_id)

    async def run():
        async with create_connected_server_and_client_session(
            _server(page_service, conversation_actor)
        ) as client:
            first = await client.call_tool(
                "guide",
                {"workspace_id": str(workspace_id), "conversation_token": "new"},
            )
            token = json.loads(first.content[0].text)["envelope"]["conversation_token"]
            second = await client.call_tool(
                "guide",
                {"workspace_id": str(workspace_id), "conversation_token": token},
            )
            return first, second, token

    first, second, token = anyio.run(run)
    for result in (first, second):
        payload = json.loads(result.content[0].text)
        assert result.isError is False
        assert payload["envelope"]["conversation_token"] == token
        assert "conversation_token_not_supplied" not in {
            warning["code"] for warning in payload["envelope"].get("warnings", [])
        }
    assert _chat_count(page_service, workspace_id) == before + 1


def test_every_write_tool_requires_a_conversation():
    """CONTENT_TOOLS gates the token requirement. Anything the chat service
    already counts as a write must be in it, or a new write tool could quietly
    skip the contract and resume splitting one chat per call."""
    from backend.mcp.scopes import TOOL_REQUIRED_SCOPES
    from backend.services.mcp_chat_service import WRITE_TOOLS

    assert WRITE_TOOLS <= CONTENT_TOOLS, WRITE_TOOLS - CONTENT_TOOLS
    for tool in CONTENT_TOOLS:
        assert tool in TOOL_REQUIRED_SCOPES, f"{tool} is not a registered tool"


def test_upload_image_is_pinned_to_the_conversation_boundary():
    assert "upload_image" in CONTENT_TOOLS

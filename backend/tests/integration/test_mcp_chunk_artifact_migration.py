from datetime import UTC, datetime
from importlib import import_module
from uuid import uuid4

from sqlalchemy import select

from backend.models.chunk import ContentChunk
from backend.models.mcp_chat import (
    McpChatArtifactLink,
    McpChatSession,
    McpChatToolCallArtifact,
)
from backend.models.mcp_trace import MCPToolCall
from backend.models.workspace import MCPClient, Workspace


def test_migration_repairs_chunk_ids_in_mcp_artifact_links(session_factory):
    migration = import_module(
        "backend.migrations.versions.0002_epic1"
    )
    page_id = uuid4()
    chunk_id = uuid4()
    first_seen = datetime(2026, 7, 1, tzinfo=UTC)
    clean_last_seen = datetime(2026, 7, 2, tzinfo=UTC)
    contaminated_last_seen = datetime(2026, 7, 3, tzinfo=UTC)

    with session_factory.begin() as session:
        workspace = session.scalar(select(Workspace).where(Workspace.slug == "default"))
        client = session.scalar(
            select(MCPClient).where(MCPClient.workspace_id == workspace.id)
        )
        clean_call = _tool_call(client.id, workspace.id)
        contaminated_call = _tool_call(client.id, workspace.id)
        session.add_all([clean_call, contaminated_call])
        session.flush()
        contaminated_call_id = contaminated_call.id
        chat = McpChatSession(
            workspace_id=workspace.id,
            client_id=client.id,
            mcp_session_id="migration-test-session",
        )
        chunk = ContentChunk(
            id=chunk_id,
            workspace_id=workspace.id,
            document_kind="context_page",
            document_id=page_id,
            content_scope="published",
            ordinal=0,
            chunk_text="chunk text",
            chunk_text_hash="chunk-hash",
            token_count=2,
        )
        session.add_all([chat, chunk])
        session.flush()
        clean_link = McpChatArtifactLink(
            workspace_id=workspace.id,
            chat_session_id=chat.id,
            resource_kind="context_page",
            resource_id=page_id,
            verb="referenced",
            first_occurred_at=clean_last_seen,
            last_occurred_at=clean_last_seen,
            occurrence_count=3,
            last_mcp_tool_call_id=clean_call.id,
        )
        contaminated_link = McpChatArtifactLink(
            workspace_id=workspace.id,
            chat_session_id=chat.id,
            resource_kind="context_page",
            resource_id=chunk_id,
            verb="referenced",
            first_occurred_at=first_seen,
            last_occurred_at=contaminated_last_seen,
            occurrence_count=2,
            last_mcp_tool_call_id=contaminated_call.id,
        )
        clean_edge = McpChatToolCallArtifact(
            workspace_id=workspace.id,
            chat_session_id=chat.id,
            mcp_tool_call_id=contaminated_call.id,
            resource_kind="context_page",
            resource_id=page_id,
            verb="referenced",
        )
        contaminated_edge = McpChatToolCallArtifact(
            workspace_id=workspace.id,
            chat_session_id=chat.id,
            mcp_tool_call_id=contaminated_call.id,
            resource_kind="context_page",
            resource_id=chunk_id,
            verb="referenced",
        )
        session.add_all(
            [clean_link, contaminated_link, clean_edge, contaminated_edge]
        )
        session.flush()

        migration._repair_mcp_chunk_artifact_links(session.connection())
        session.expire_all()

        links = session.scalars(select(McpChatArtifactLink)).all()
        edges = session.scalars(select(McpChatToolCallArtifact)).all()

    assert len(links) == 1
    assert links[0].resource_id == page_id
    assert links[0].occurrence_count == 5
    assert links[0].first_occurred_at == first_seen
    assert links[0].last_occurred_at == contaminated_last_seen
    assert links[0].last_mcp_tool_call_id == contaminated_call_id
    assert len(edges) == 1
    assert edges[0].resource_id == page_id


def _tool_call(client_id, workspace_id):
    return MCPToolCall(
        client_id=client_id,
        workspace_id=workspace_id,
        request_id=uuid4(),
        tool_name="search",
        result_status="succeeded",
    )

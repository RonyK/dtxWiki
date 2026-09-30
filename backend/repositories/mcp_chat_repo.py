"""Workspace-scoped reads over MCP chat traces for the Chats projection."""

from __future__ import annotations

from collections.abc import Iterable
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from backend.models.mcp_chat import McpChatToolCallArtifact
from backend.models.mcp_trace import MCPToolCall


def list_call_artifact_edges(
    session: Session,
    *,
    workspace_id: UUID,
    chat_session_id: UUID,
) -> list[tuple[UUID, str, str, UUID]]:
    """Every (call, verb, kind, resource) edge of one chat, stably ordered.

    Ordered by occurrence then resource so a call's linked artifacts, and the
    one that headlines it, are the same on every read.
    """

    rows = session.execute(
        select(
            McpChatToolCallArtifact.mcp_tool_call_id,
            McpChatToolCallArtifact.verb,
            McpChatToolCallArtifact.resource_kind,
            McpChatToolCallArtifact.resource_id,
        )
        .where(
            McpChatToolCallArtifact.workspace_id == workspace_id,
            McpChatToolCallArtifact.chat_session_id == chat_session_id,
        )
        .order_by(
            McpChatToolCallArtifact.occurred_at,
            McpChatToolCallArtifact.resource_id,
        )
    ).all()
    return [
        (tool_call_id, verb, resource_kind, resource_id)
        for tool_call_id, verb, resource_kind, resource_id in rows
    ]


def list_calls_naming_page(
    session: Session,
    *,
    workspace_id: UUID,
    page_id: UUID,
    tool_names: Iterable[str],
) -> list[MCPToolCall]:
    """Calls of ``tool_names`` whose input or recorded result names ``page_id``.

    Matches the captured input's ``page_id`` or the ``page_id`` capture kept
    from the response in ``result_detail`` (a create names its new page only
    there). Failed calls have no artifact edge, so this deliberately reads the
    trace itself rather than the edge table.
    """

    page = str(page_id)
    return list(
        session.scalars(
            select(MCPToolCall).where(
                MCPToolCall.workspace_id == workspace_id,
                MCPToolCall.tool_name.in_(sorted(tool_names)),
                or_(
                    MCPToolCall.input_summary["page_id"].as_string() == page,
                    MCPToolCall.result_detail["page_id"].as_string() == page,
                ),
            )
        ).all()
    )

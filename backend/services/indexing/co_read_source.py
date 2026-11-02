"""One eligibility/query boundary for focused body-view evidence."""

from collections.abc import Iterator
from datetime import datetime
from uuid import UUID

from sqlalchemy import tuple_
from sqlalchemy.orm import Session

from backend.models.mcp_chat import McpChatArtifactVerb, McpChatToolCallArtifact
from backend.models.mcp_trace import MCPToolCall
from backend.policy.mcp_scopes import FOCUSED_READ_TOOLS
from backend.repositories.workspace_scoped import join_in_workspace, select_in_workspace
from backend.services.entity_ref import EntityKind
from backend.services.indexing.co_read import Read
from backend.services.indexing.co_read_policy import CO_ACCESS_SOURCE_BATCH_ROWS

CO_ACCESS_SOURCE_VERBS = frozenset({McpChatArtifactVerb.VIEWED})
# The singleton equality matches the viewed-session partial index predicate.
# Unpacking rejects an expanded allowlist until the query/index policy changes.
(CO_ACCESS_SOURCE_VERB,) = CO_ACCESS_SOURCE_VERBS


def co_read_statement(
    workspace_id: UUID,
    *,
    chat_session_id: UUID | None = None,
    since: datetime | None = None,
    after_key: tuple[datetime, UUID, UUID] | None = None,
    conditions=(),
):
    predicates = [
        McpChatToolCallArtifact.verb == CO_ACCESS_SOURCE_VERB,
        McpChatToolCallArtifact.resource_kind.in_([kind.value for kind in EntityKind]),
        *conditions,
    ]
    if chat_session_id is not None:
        predicates.append(McpChatToolCallArtifact.chat_session_id == chat_session_id)
    if since is not None:
        predicates.append(McpChatToolCallArtifact.occurred_at >= since)
    if after_key is not None:
        predicates.append(
            tuple_(
                McpChatToolCallArtifact.occurred_at,
                McpChatToolCallArtifact.mcp_tool_call_id,
                McpChatToolCallArtifact.id,
            )
            > after_key
        )
    return join_in_workspace(
        select_in_workspace(
            McpChatToolCallArtifact, workspace_id=workspace_id, conditions=predicates
        ),
        McpChatToolCallArtifact,
        MCPToolCall,
        workspace_id=workspace_id,
        on=(MCPToolCall.id == McpChatToolCallArtifact.mcp_tool_call_id)
        & (MCPToolCall.result_status == "succeeded")
        & MCPToolCall.tool_name.in_(FOCUSED_READ_TOOLS),
    )


def co_read_rows(session: Session, workspace_id: UUID, **filters):
    return session.scalars(
        co_read_statement(workspace_id, **filters)
        .order_by(
            McpChatToolCallArtifact.chat_session_id,
            McpChatToolCallArtifact.occurred_at,
            McpChatToolCallArtifact.mcp_tool_call_id,
            McpChatToolCallArtifact.id,
        )
        .execution_options(
            yield_per=CO_ACCESS_SOURCE_BATCH_ROWS, populate_existing=True
        )
    )


def co_read_source(session: Session, workspace_id: UUID, **filters) -> Iterator[Read]:
    rows = co_read_rows(session, workspace_id, **filters)
    try:
        for row in rows:
            yield Read(
                row.chat_session_id,
                (row.resource_kind, row.resource_id),
                row.occurred_at,
                row.mcp_tool_call_id,
                row.id,
            )
    finally:
        rows.close()

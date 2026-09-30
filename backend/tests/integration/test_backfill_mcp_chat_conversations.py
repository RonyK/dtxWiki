from datetime import timedelta
from uuid import uuid4

from sqlalchemy import select

from backend.models.audit import AuditEvent
from backend.models.base import utc_now
from backend.models.mcp_chat import McpChatArtifactLink, McpChatSession
from backend.models.user import User
from backend.models.workspace import MCPClient, Workspace
from backend.scripts.backfill_mcp_chat_conversations import (
    backfill_conversation_groups,
)


def _legacy_chat(session, *, workspace, client, owner, token, start, resource_id):
    chat = McpChatSession(
        workspace_id=workspace.id,
        client_id=client.id,
        owner_user_id=owner.id,
        mcp_session_id=token,
        started_at=start,
        last_activity_at=start + timedelta(seconds=10),
        tool_call_count=1,
        write_count=1,
        error_count=0,
        title="Updated Project Plan",
        title_user_edited=False,
    )
    session.add(chat)
    session.flush()
    session.add(
        McpChatArtifactLink(
            workspace_id=workspace.id,
            chat_session_id=chat.id,
            resource_kind="context_page",
            resource_id=resource_id,
            verb="edited",
            first_occurred_at=start,
            last_occurred_at=start,
            occurrence_count=1,
            last_mcp_tool_call_id=uuid4(),
        )
    )
    return chat


def test_chat_backfill_is_dry_run_by_default_and_audits_each_applied_session(
    session_factory,
):
    with session_factory.begin() as session:
        workspace = session.scalar(select(Workspace).where(Workspace.slug == "default"))
        owner = session.scalars(select(User)).first()
        assert workspace is not None
        client = MCPClient(
            workspace_id=workspace.id,
            name="chat-backfill-test-client",
            client_type="api_token",
            scopes=["mcp:read", "mcp:write"],
            status="active",
        )
        session.add(client)
        session.flush()
        if owner is None:
            owner = User(
                email="chat-backfill@example.com",
                password_hash="test-only",
                display_name="Chat Backfill",
                status="active",
            )
            session.add(owner)
            session.flush()
        resource_id = uuid4()
        started = utc_now() - timedelta(days=1)
        first = _legacy_chat(
            session,
            workspace=workspace,
            client=client,
            owner=owner,
            token="legacy-1",
            start=started,
            resource_id=resource_id,
        )
        second = _legacy_chat(
            session,
            workspace=workspace,
            client=client,
            owner=owner,
            token="legacy-2",
            start=started + timedelta(seconds=35),
            resource_id=resource_id,
        )
        too_late = _legacy_chat(
            session,
            workspace=workspace,
            client=client,
            owner=owner,
            token="separate-conversation",
            start=started + timedelta(minutes=5),
            resource_id=resource_id,
        )
        session.flush()

        dry_run = backfill_conversation_groups(session)
        assert len(dry_run) == 1
        assert dry_run[0].session_ids == (first.id, second.id)
        assert dry_run[0].gaps_seconds == (25,)
        assert first.conversation_group_id is None
        assert second.conversation_group_id is None
        assert too_late.conversation_group_id is None

        applied = backfill_conversation_groups(session, apply=True)
        assert applied == dry_run
        assert first.conversation_group_id == dry_run[0].conversation_group_id
        assert second.conversation_group_id == dry_run[0].conversation_group_id
        assert too_late.conversation_group_id is None
        events = list(
            session.scalars(
                select(AuditEvent).where(
                    AuditEvent.action
                    == "mcp_chat.conversation_group_backfilled"
                )
            ).all()
        )
        assert {event.target_id for event in events} == {first.id, second.id}
        assert all(event.payload["heuristic"] for event in events)

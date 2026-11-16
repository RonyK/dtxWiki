import json
from datetime import UTC, datetime, timedelta
from itertools import count
from uuid import uuid4

import anyio
import pytest
from mcp.shared.memory import create_connected_server_and_client_session
from sqlalchemy import select

from backend.app.settings import Settings
from backend.mcp.protocol import runner as runner_module
from backend.mcp.protocol.server import create_mcp_server
from backend.models.change import ChangeSet
from backend.models.context import ContextPageVersion
from backend.models.mcp_chat import McpChatSession
from backend.policy.auth import ActorContext
from backend.services.context_write_service import (
    stale_pages_for_conversation,
)


@pytest.fixture
def nag_actor(mcp_default_actor) -> ActorContext:
    return mcp_default_actor(
        actor_id="nag-channel-test",
        scopes=("mcp:read", "mcp:write"),
    )


def _server(page_service, actor: ActorContext):
    return create_mcp_server(
        settings=Settings(database_url="sqlite+pysqlite:///:memory:"),
        actor=actor,
        page_service=page_service,
    )


def _payload(result):
    payload = json.loads(result.content[0].text)
    assert result.isError is False, payload
    assert payload["envelope"]["result_status"] == "succeeded", payload
    return payload


def _structure_map_args(workspace_id, page_id, conversation_token):
    return {
        "workspace_id": str(workspace_id),
        "page_id": str(page_id),
        "conversation_token": conversation_token,
    }


def _warning_codes(payload):
    return {warning["code"] for warning in payload["envelope"].get("warnings", [])}


def _warning_with_code(payload, code):
    return next(
        warning
        for warning in payload["envelope"].get("warnings", [])
        if warning["code"] == code
    )


def test_stale_read_warning_is_bounded_scoped_and_clears_after_reread(
    page_service,
    nag_actor,
    mcp_default_actor,
    monkeypatch,
):
    # A read moves the conversation's read pointer only if it completed no
    # earlier than the read the pointer already holds (a trace delayed behind a
    # newer one must not rewind it), and the runner takes that time from the
    # wall clock. The clock is not monotonic: a step back between the first read
    # and the re-read leaves the pointer on the old version, and the warning the
    # re-read is meant to clear stays. Give the reads an explicit chronology, one
    # tick per sample, so this test covers the warning, not the host clock.
    # Production still cannot tell a stepped-back read from a delayed one; that
    # needs an ordering of observations that survives a clock step.
    # Start from the real time once, so the conversation still counts as active
    # (an epoch in the past would freeze it as ended during capture).
    trace_epoch = datetime.now(UTC)
    trace_ticks = count()
    monkeypatch.setattr(
        runner_module,
        "_utc_now",
        lambda: trace_epoch + timedelta(seconds=next(trace_ticks)),
    )
    workspace_id = nag_actor.workspace_id
    assert workspace_id is not None
    different_owner = mcp_default_actor(
        actor_id="different-owner",
        scopes=("mcp:read",),
    )
    assert different_owner.user_id is not None
    assert different_owner.user_id != nag_actor.user_id
    body = (
        "# Title\n\n" + "\n\n".join(f"paragraph {index}" for index in range(10)) + "\n"
    )
    advanced_body = (
        "# Title\n\n"
        + "\n\n".join(f"paragraph {index}, externally edited" for index in range(10))
        + "\n"
    )
    page = page_service.write_page(
        workspace_slug="default",
        slug="conversation-stale-warning",
        title="Conversation Stale Warning",
        body_md=body,
        actor_kind="human",
        actor_id="00000000-0000-0000-0000-000000000010",
        actor_provenance="browser_session",
    )
    unrelated = page_service.write_page(
        workspace_slug="default",
        slug="conversation-stale-unrelated",
        title="Conversation Stale Unrelated",
        body_md="# Other\n\nunchanged\n",
        actor_kind="human",
        actor_id="00000000-0000-0000-0000-000000000010",
        actor_provenance="browser_session",
    )

    async def run():
        async with create_connected_server_and_client_session(
            _server(page_service, nag_actor)
        ) as client:
            initial = _payload(
                await client.call_tool(
                    "read_structure_map",
                    _structure_map_args(workspace_id, page.id, "new"),
                )
            )
            token = initial["envelope"]["conversation_token"]
            advanced = page_service.write_page(
                workspace_slug="default",
                slug=page.slug,
                title=page.title,
                body_md=advanced_body,
                actor_kind="human",
                actor_id="00000000-0000-0000-0000-000000000010",
                actor_provenance="browser_session",
                base_version_id=page.version_id,
                base_content_hash=page.content_hash,
            )
            with page_service.session_factory.begin() as session:
                changed_in_chat = session.scalar(
                    select(McpChatSession).where(
                        McpChatSession.workspace_id == workspace_id,
                        McpChatSession.mcp_session_id == token,
                    )
                )
                assert changed_in_chat is not None
                change_set = ChangeSet(
                    workspace_id=workspace_id,
                    reason="stale warning actor provenance fixture",
                    trigger_type="test",
                    actor_type="human",
                    actor_id=None,
                    origin_chat_session_id=changed_in_chat.id,
                    status="draft_created",
                    risk_level="low",
                    cite_call_skipped=False,
                )
                session.add(change_set)
                session.flush()
                advanced_version = session.scalar(
                    select(ContextPageVersion).where(
                        ContextPageVersion.workspace_id == workspace_id,
                        ContextPageVersion.page_id == page.id,
                        ContextPageVersion.id == advanced.version_id,
                    )
                )
                assert advanced_version is not None
                advanced_version.change_set_id = change_set.id
                changed_in_chat_id = changed_in_chat.id
            stale = _payload(
                await client.call_tool(
                    "read_structure_map",
                    _structure_map_args(workspace_id, unrelated.id, token),
                )
            )
            with page_service.session_factory() as session:
                wrong_owner_pages = stale_pages_for_conversation(
                    session,
                    workspace_id=workspace_id,
                    conversation_token=token,
                    actor=different_owner,
                )
                wrong_workspace_pages = stale_pages_for_conversation(
                    session,
                    workspace_id=uuid4(),
                    conversation_token=token,
                    actor=nag_actor,
                )
            other_conversation = _payload(
                await client.call_tool(
                    "read_structure_map",
                    _structure_map_args(workspace_id, unrelated.id, "new"),
                )
            )
            reread = _payload(
                await client.call_tool(
                    "read_structure_map",
                    _structure_map_args(workspace_id, page.id, token),
                )
            )
            cleared = _payload(
                await client.call_tool(
                    "read_structure_map",
                    _structure_map_args(workspace_id, unrelated.id, token),
                )
            )
            return (
                initial,
                advanced,
                stale,
                other_conversation,
                reread,
                cleared,
                wrong_owner_pages,
                wrong_workspace_pages,
                changed_in_chat_id,
            )

    (
        initial,
        advanced,
        stale,
        other_conversation,
        reread,
        cleared,
        wrong_owner_pages,
        wrong_workspace_pages,
        changed_in_chat_id,
    ) = anyio.run(run)

    warning = _warning_with_code(stale, "conversation_read_stale")
    assert str(page.id) in warning["message"]
    assert initial["document"]["version_id"] in warning["message"]
    assert str(advanced.version_id) in warning["message"]
    assert "changed by" in warning["message"]
    assert "(human)" in warning["message"]
    assert f"conversation {changed_in_chat_id}" in warning["message"]
    # The superseded read version keeps its archived map, so the warning names
    # real changed blocks. Every paragraph was rewritten; only the heading's ID
    # is shared, and the message lists a bounded slice of the rest.
    read_ids = {block["id"] for block in initial["blocks"]}
    with page_service.session_factory() as session:
        head_ids = {
            entry["id"]
            for entry in session.get(ContextPageVersion, advanced.version_id).block_map
        }
    changed = read_ids ^ head_ids
    listed = (
        warning["message"]
        .split("changed block IDs: ", 1)[1]
        .split(" (+", 1)[0]
        .split(", ")
    )
    assert listed and set(listed) <= changed
    assert f"(+{len(changed) - len(listed)} omitted)" in warning["message"]
    assert len(warning["message"]) < 2_000
    assert wrong_owner_pages == []
    assert wrong_workspace_pages == []
    assert "conversation_read_stale" not in _warning_codes(other_conversation)
    # Warning lookup happens before the successful read trace advances the
    # ledger, so the read that heals it still carries the useful notification.
    assert "conversation_read_stale" in _warning_codes(reread)
    assert "conversation_read_stale" not in _warning_codes(cleared)

import json
from uuid import uuid4

import anyio
import pytest
from mcp.shared.memory import create_connected_server_and_client_session
from sqlalchemy import select

from backend.app.settings import Settings
from backend.mcp.protocol.server import create_mcp_server
from backend.models.change import ChangeSet
from backend.models.context import ContextPageVersion
from backend.models.mcp_chat import McpChatSession
from backend.policy.auth import ActorContext
from backend.schemas.context_edit import PatchDraftRequest
from backend.services.context_write_service import (
    ContextWriteService,
    stale_pages_for_conversation,
)

# P8.6: patch_draft is no longer a callable MCP tool. test_conversation_nag_
# tracks_a_diverged_branch_until_reconciliation below needs a graph_status=
# "branched" page version whose ChangeSet.trigger_type is
# "mcp_patch_draft_diverged" -- that shape is written only by
# ContextWriteService._patch_draft_once (context_write_service.py), and no
# other currently-live MCP tool reaches that code path: patch_context_edit
# and friends go through PageService's separate explicit-editing-version
# lifecycle (no base/push/diverge concept at all), and
# ReviewService.submit_revision_for_review calls .update_draft, not
# .patch_draft. So the branch this test needs is now only constructible via a
# direct service call -- see _patch_draft below. The nag-detection reads
# (read_structure_map) stay on the live MCP client, since the
# unresolved_diverged_branch lookup itself (backend/mcp/protocol/runner.py)
# runs generically after ANY dispatched tool call, not just patch_draft, and
# is unaffected by patch_draft's deregistration.


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
        "target": {"page_id": str(page_id)},
        "conversation_token": conversation_token,
    }


def _warning(payload):
    return next(
        warning
        for warning in payload["envelope"]["warnings"]
        if warning["code"] == "unresolved_diverged_branch"
    )


def _warning_codes(payload):
    return {warning["code"] for warning in payload["envelope"]["warnings"]}


def _warning_with_code(payload, code):
    return next(
        warning
        for warning in payload["envelope"]["warnings"]
        if warning["code"] == code
    )


def _patch_draft(
    page_service,
    workspace_id,
    actor: ActorContext,
    *,
    origin_chat_session_id=None,
    **payload,
):
    """Engine-level equivalent of ``client.call_tool("patch_draft", {...})``."""
    with page_service.session_factory.begin() as session:
        return ContextWriteService().patch_draft(
            session=session,
            workspace_id=workspace_id,
            request=PatchDraftRequest(**payload),
            actor_kind=actor.actor_kind,
            actor_id=actor.actor_id,
            origin_chat_session_id=origin_chat_session_id,
        )


# P8.6: test_patch_advances_conversation_base_and_replay_does_not_rewind was
# removed here -- it asserted patch_draft's own MCP-dispatch side effects
# (MCPToolCall rows, chat-artifact-link verb/rollups, conversation-base
# replay), which are gone with the deregistered tool; equivalent
# idempotent-replay-via-dispatch coverage for the replacement tools lives in
# backend/tests/mcp_contract/test_context_edit_tools.py
# (test_patch_context_edit_replay_is_idempotent and friends).


def test_stale_read_warning_is_bounded_scoped_and_clears_after_reread(
    page_service,
    nag_actor,
    mcp_default_actor,
):
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
    assert "omitted" in warning["message"]
    assert len(warning["message"]) < 2_000
    assert wrong_owner_pages == []
    assert wrong_workspace_pages == []
    assert "conversation_read_stale" not in _warning_codes(other_conversation)
    # Warning lookup happens before the successful read trace advances the
    # ledger, so the read that heals it still carries the useful notification.
    assert "conversation_read_stale" in _warning_codes(reread)
    assert "conversation_read_stale" not in _warning_codes(cleared)


def test_conversation_nag_tracks_a_diverged_branch_until_reconciliation(
    page_service,
    nag_actor,
):
    workspace_id = nag_actor.workspace_id
    assert workspace_id is not None
    page = page_service.write_page(
        workspace_slug="default",
        slug="nag-branch",
        title="Nag Branch",
        body_md="# Title\n\nfirst\n",
        actor_kind="human",
        actor_id="00000000-0000-0000-0000-000000000010",
        actor_provenance="browser_session",
    )
    unrelated_page = page_service.write_page(
        workspace_slug="default",
        slug="nag-unrelated",
        title="Nag Unrelated",
        body_md="# Other\n\nunrelated\n",
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
            conversation_token = initial["envelope"]["conversation_token"]
            paragraph = next(
                block for block in initial["blocks"] if block["kind"] == "p"
            )
            _payload(
                await client.call_tool(
                    "read_structure_map",
                    _structure_map_args(
                        workspace_id,
                        unrelated_page.id,
                        conversation_token,
                    ),
                )
            )
            page_service.write_page(
                workspace_slug="default",
                slug=unrelated_page.slug,
                title=unrelated_page.title,
                body_md="# Other\n\nunrelated, externally changed\n",
                actor_kind="human",
                actor_id="00000000-0000-0000-0000-000000000010",
                actor_provenance="browser_session",
                base_version_id=unrelated_page.version_id,
                base_content_hash=unrelated_page.content_hash,
            )

            page_service.write_page(
                workspace_slug="default",
                slug="nag-branch",
                title="Nag Branch",
                body_md="# Title\n\nfirst, changed by someone else\n",
                actor_kind="human",
                actor_id="00000000-0000-0000-0000-000000000010",
                actor_provenance="browser_session",
                base_version_id=page.version_id,
                base_content_hash=page.content_hash,
            )

            with page_service.session_factory() as session:
                chat_session_id = session.scalar(
                    select(McpChatSession.id).where(
                        McpChatSession.workspace_id == workspace_id,
                        McpChatSession.mcp_session_id == conversation_token,
                    )
                )
                assert chat_session_id is not None

            diverged = _patch_draft(
                page_service,
                workspace_id,
                nag_actor,
                origin_chat_session_id=chat_session_id,
                page_id=page.id,
                reason="create unresolved branch",
                base=initial["document"]["version_id"],
                operations=[
                    {
                        "op": "replace_block",
                        "block": paragraph["id"],
                        "expected": paragraph["hash"],
                        "content": "first, my edit",
                    }
                ],
            )
            branch_id = str(diverged.alternative_draft_id)

            same_conversation = _payload(
                await client.call_tool(
                    "read_structure_map",
                    _structure_map_args(
                        workspace_id,
                        unrelated_page.id,
                        conversation_token,
                    ),
                )
            )
            other_conversation = _payload(
                await client.call_tool(
                    "read_structure_map",
                    _structure_map_args(workspace_id, unrelated_page.id, "new"),
                )
            )
            head = _payload(
                await client.call_tool(
                    "read_structure_map",
                    _structure_map_args(workspace_id, page.id, conversation_token),
                )
            )
            conflict = diverged.conflicts[0]
            reconciled = _patch_draft(
                page_service,
                workspace_id,
                nag_actor,
                origin_chat_session_id=chat_session_id,
                page_id=page.id,
                reason="reconcile unresolved branch",
                base=head["document"]["version_id"],
                reconciles=[branch_id],
                operations=[
                    {
                        "op": "resolve_conflict",
                        "block": conflict["block_id"],
                        "resolution": "keep_head",
                    }
                ],
            )
            after_reconciliation = _payload(
                await client.call_tool(
                    "read_structure_map",
                    _structure_map_args(
                        workspace_id,
                        unrelated_page.id,
                        conversation_token,
                    ),
                )
            )
            return (
                diverged,
                same_conversation,
                other_conversation,
                reconciled,
                after_reconciliation,
                branch_id,
            )

    (
        diverged,
        same_conversation,
        other_conversation,
        reconciled,
        after_reconciliation,
        branch_id,
    ) = anyio.run(run)

    # `diverged` and `reconciled` are now direct ContextWriteService.patch_draft()
    # calls (patch_draft is no longer a callable MCP tool -- see the comment
    # above the imports), so neither carries an MCP envelope/warnings of its own.
    # The unresolved-diverged-branch nag is computed generically for ANY
    # dispatched tool call in this conversation (backend/mcp/protocol/runner.py),
    # so the message-content assertions move to `same_conversation`, the first
    # live tool call issued after the branch exists -- it goes through the exact
    # same unresolved_diverged_branch_warning_message() call the direct diverge
    # call would have produced a warning from, so this is a later check, not a
    # weaker one. NOT preserved: the original assertion that "conversation_read_
    # stale" co-occurred on the diverge response ITSELF, and that "unresolved_
    # diverged_branch" was absent from the reconcile response ITSELF -- both
    # specifically asserted MCP-dispatch/envelope behavior of patch_draft, which
    # no longer runs at all now that the tool is deregistered.
    assert diverged.push_outcome == "diverged"
    same_conversation_warning = _warning(same_conversation)
    assert "unresolved_diverged_branch" in _warning_codes(same_conversation)
    assert page.slug in same_conversation_warning["message"]
    assert branch_id in same_conversation_warning["message"]
    assert (
        f"/api/v1/workspaces/{workspace_id}/context/pages/{page.id}"
        f"/versions/{branch_id}/reconciliation" in same_conversation_warning["message"]
    )
    assert (
        f"/api/v1/workspaces/{workspace_id}/context/pages/{page.id}"
        f"/versions/{branch_id}/discard" in same_conversation_warning["message"]
    )
    assert "unresolved_diverged_branch" not in _warning_codes(other_conversation)
    assert "unresolved_diverged_branch" not in _warning_codes(after_reconciliation)


def test_fresh_conversation_read_has_no_nag_before_a_chat_session_exists(
    page_service,
    nag_actor,
):
    workspace_id = nag_actor.workspace_id
    assert workspace_id is not None
    page = page_service.write_page(
        workspace_slug="default",
        slug="nag-fresh-conversation",
        title="Nag Fresh Conversation",
        body_md="# Title\n\nfresh\n",
        actor_kind="human",
        actor_id="00000000-0000-0000-0000-000000000010",
        actor_provenance="browser_session",
    )

    async def run():
        async with create_connected_server_and_client_session(
            _server(page_service, nag_actor)
        ) as client:
            return _payload(
                await client.call_tool(
                    "read_structure_map",
                    _structure_map_args(workspace_id, page.id, "new"),
                )
            )

    payload = anyio.run(run)

    assert "unresolved_diverged_branch" not in _warning_codes(payload)

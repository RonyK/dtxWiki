"""Real context-edit calls, traced by the MCP runner, as Chats shows them.

The unit tests build trace rows by hand. This one drives create/patch through
``call_tool_async`` so the stored input previews, the response's edit receipt
and the version the edit landed on are exactly what capture writes, then reads
the chat back the way the detail screen does.
"""

from __future__ import annotations

import json
from uuid import uuid4

import anyio
from sqlalchemy import select

from backend.mcp.protocol.runner import call_tool_async
from backend.models.mcp_chat import McpChatSession
from backend.models.workspace import Workspace
from backend.policy.auth import ActorContext
from backend.services.mcp_chat_service import McpChatService


def _call(page_service, actor, name, arguments):
    async def run():
        return await call_tool_async(
            name,
            arguments,
            actor=actor,
            page_service=page_service,
            session_factory=page_service.session_factory,
        )

    result = anyio.run(run)
    return json.loads(result.content[0].text)


def _reader(user_id, workspace_id, role) -> ActorContext:
    return ActorContext(
        actor_kind="human",
        actor_id=str(user_id),
        user_id=user_id,
        token_kind="session",
        scopes=("read",),
        workspace_id=workspace_id,
        workspace_role=role,
        role=role,
    )


def test_traced_edit_calls_carry_previews_receipts_and_the_session_diff(
    page_service, mcp_default_actor
):
    with page_service.session_factory() as session:
        workspace_id = session.scalar(
            select(Workspace).where(Workspace.slug == "default")
        ).id
    user_id = uuid4()
    # Production bearer actors are the user themself (actor_id == user id), so
    # the editing row the client opens is the same person's in the browser.
    actor = mcp_default_actor(scopes=("*",), actor_id=str(user_id), user_id=user_id)

    # Writes refuse to start a conversation; the first call mints the token.
    token = _call(page_service, actor, "guide", {"workspace_id": str(workspace_id)})[
        "envelope"
    ]["conversation_token"]
    created = _call(
        page_service,
        actor,
        "create_context_page",
        {
            "workspace_id": str(workspace_id),
            "title": "E2E Chats Page",
            "body_md": "# E2E Chats Page\n\nfirst line\n",
            "conversation_token": token,
        },
    )
    assert created["envelope"]["result_status"] == "succeeded", created
    page = created["data"]

    patched = _call(
        page_service,
        actor,
        "patch_context_edit",
        {
            "workspace_id": str(workspace_id),
            "page_id": page["id"],
            "version_id": page["version_id"],
            "expected_editing_revision": page["editing_revision"],
            "idempotency_key": f"e2e-patch-{uuid4()}",
            "conversation_token": token,
            "operations": [
                {
                    "op": "replace_text",
                    "old_text": "first line",
                    "new_text": "second line",
                }
            ],
        },
    )
    assert patched["envelope"]["result_status"] == "succeeded", patched
    after_patch = patched["data"]

    failed = _call(
        page_service,
        actor,
        "patch_context_edit",
        {
            "workspace_id": str(workspace_id),
            "page_id": page["id"],
            "version_id": page["version_id"],
            "expected_editing_revision": after_patch["editing_revision"],
            "idempotency_key": f"e2e-fail-{uuid4()}",
            "conversation_token": token,
            "operations": [
                {
                    "op": "replace_text",
                    "old_text": "text that is not there",
                    "new_text": "never applied",
                }
            ],
        },
    )
    assert failed["envelope"]["result_status"] != "succeeded", failed

    overlapping = _call(
        page_service,
        actor,
        "patch_context_edit",
        {
            "workspace_id": str(workspace_id),
            "page_id": page["id"],
            "version_id": page["version_id"],
            "expected_editing_revision": after_patch["editing_revision"],
            "idempotency_key": f"e2e-overlap-{uuid4()}",
            "conversation_token": token,
            "operations": [
                {"op": "replace_text", "old_text": "second line", "new_text": "a"},
                {"op": "replace_text", "old_text": "second line", "new_text": "b"},
            ],
        },
    )
    assert overlapping["envelope"]["error_code"] == "operations_overlap", overlapping

    service = McpChatService()
    with page_service.session_factory() as session:
        chat = session.scalar(
            select(McpChatSession).where(
                McpChatSession.workspace_id == workspace_id,
                McpChatSession.mcp_session_id == token,
            )
        )
        assert chat is not None
        write_count = chat.write_count
        owner_view = service.read_chat(
            session=session,
            workspace_id=workspace_id,
            chat_session_id=chat.id,
            actor=_reader(user_id, workspace_id, "owner"),
        )
        other_view = service.read_chat(
            session=session,
            workspace_id=workspace_id,
            chat_session_id=chat.id,
            actor=_reader(uuid4(), workspace_id, "editor"),
        )

    assert write_count == 4
    _guide_row, create_row, patch_row, failed_row, overlap_row = owner_view.timeline

    assert create_row.edit_summary.body_preview.text == (
        "# E2E Chats Page\n\nfirst line"
    )
    assert patch_row.redaction_reason is None
    assert [
        (preview.field, preview.text)
        for preview in patch_row.edit_summary.operations[0].previews
    ] == [("old_text", "first line"), ("new_text", "second line")]
    assert patch_row.edit_summary.source == "receipt"
    assert patch_row.result_detail.edit_receipt.edit_mode == "patch"
    # A brand-new page: its open session diffs against an empty document,
    # offered once, on the session's latest successful edit.
    assert create_row.session_diff is None
    assert patch_row.session_diff is not None
    assert patch_row.session_diff.from_version_id is None
    assert str(patch_row.session_diff.to_version_id) == page["version_id"]

    assert failed_row.error_code
    assert failed_row.redaction_reason is None
    assert failed_row.result_summary
    assert failed_row.edit_summary.operations[0].previews[0].text == (
        "text that is not there"
    )

    # The collision is kept as structure, parsed from the server's own detail.
    assert [
        (
            detail.code,
            detail.conflict.left_op_index,
            detail.conflict.right_op_index,
            detail.conflict.overlap_kind,
        )
        for detail in overlap_row.error_details
    ] == [("operations_overlap", 0, 1, "same_range")]
    assert overlap_row.error_details[0].conflict.left_lines is not None

    # Nobody else can see this unshared page, so every row is shape only.
    assert [row.redaction_reason for row in other_view.timeline[1:]] == [
        "target_hidden",
        "target_hidden",
        "target_hidden",
        "target_hidden",
    ]
    hidden_overlap = other_view.timeline[-1].error_details[0].conflict
    assert (hidden_overlap.left_op_index, hidden_overlap.right_op_index) == (0, 1)
    assert hidden_overlap.left_lines is None
    serialized = other_view.model_dump_json()
    for secret in ("E2E Chats Page", "first line", "second line", "not there"):
        assert secret not in serialized


def test_discarding_an_edit_deletes_the_previews_its_calls_kept(
    page_service, mcp_default_actor
):
    """The author threw the text away, so the excerpts of it that Chats kept
    beside each call go with it -- sizes and digests stay as the record."""

    from backend.models.mcp_trace import MCPToolCall

    with page_service.session_factory() as session:
        workspace_id = session.scalar(
            select(Workspace).where(Workspace.slug == "default")
        ).id
    user_id = uuid4()
    actor = mcp_default_actor(scopes=("*",), actor_id=str(user_id), user_id=user_id)
    token = _call(page_service, actor, "guide", {"workspace_id": str(workspace_id)})[
        "envelope"
    ]["conversation_token"]
    created = _call(
        page_service,
        actor,
        "create_context_page",
        {
            "workspace_id": str(workspace_id),
            "title": "Discarded Chats Page",
            "body_md": "# Discarded\n\nDISCARD-BODY-TEXT\n",
            "conversation_token": token,
        },
    )
    page = created["data"]
    patched = _call(
        page_service,
        actor,
        "patch_context_edit",
        {
            "workspace_id": str(workspace_id),
            "page_id": page["id"],
            "version_id": page["version_id"],
            "expected_editing_revision": page["editing_revision"],
            "idempotency_key": f"discard-patch-{uuid4()}",
            "conversation_token": token,
            "operations": [
                {
                    "op": "replace_text",
                    "old_text": "DISCARD-BODY-TEXT",
                    "new_text": "DISCARD-NEW-TEXT",
                }
            ],
        },
    )
    assert patched["envelope"]["result_status"] == "succeeded", patched
    discarded = _call(
        page_service,
        actor,
        "discard_context_edit",
        {
            "workspace_id": str(workspace_id),
            "page_id": page["id"],
            "version_id": page["version_id"],
            "idempotency_key": f"discard-{uuid4()}",
            "conversation_token": token,
        },
    )
    assert discarded["envelope"]["result_status"] == "succeeded", discarded

    with page_service.session_factory() as session:
        calls = session.scalars(
            select(MCPToolCall).where(
                MCPToolCall.workspace_id == workspace_id,
                MCPToolCall.tool_name.in_(
                    ("create_context_page", "patch_context_edit")
                ),
            )
        ).all()
        stored = json.dumps(
            [[call.input_summary, call.result_detail] for call in calls]
        )
        chat = session.scalar(
            select(McpChatSession).where(
                McpChatSession.workspace_id == workspace_id,
                McpChatSession.mcp_session_id == token,
            )
        )
        owner_view = McpChatService().read_chat(
            session=session,
            workspace_id=workspace_id,
            chat_session_id=chat.id,
            actor=_reader(user_id, workspace_id, "owner"),
        )

    assert len(calls) == 2
    for secret in ("DISCARD-BODY-TEXT", "DISCARD-NEW-TEXT"):
        assert secret not in stored
    assert '"old_text_digest"' in stored
    # The page was never published, so it is archived and hidden -- but it is
    # the reader's own, in their own chat: each of its rows keeps its shape
    # and a discarded-edit marker, and nothing of the page it threw away.
    edit_rows = [
        row
        for row in owner_view.timeline
        if row.tool_name in {"create_context_page", "patch_context_edit"}
    ]
    assert len(edit_rows) == 2
    for row in edit_rows:
        assert row.redaction_reason is None
        assert [item.key for item in row.result_display_items] == ["discarded_edit"]
        assert row.linked_artifacts == []
    assert edit_rows[1].edit_summary.operation_counts == {"replace_text": 1}
    serialized = owner_view.model_dump_json()
    for secret in ("Discarded Chats Page", "Discarded", page["id"]):
        assert secret not in serialized


PRIVATE_STRINGS = (
    "PRIVATE TITLE",
    "private-tag",
    "private-alias",
    "Private Heading",
    "Private paragraph",
    "PRIVATE PATCH TEXT",
    "PRIVATE NEW PAGE",
)


def test_a_private_edit_of_a_published_page_stays_its_authors(
    page_service, mcp_default_actor
):
    """Editor B can see the published page, so B sees that A's calls edited
    it -- but A's editing session is private. Nothing A wrote into it (title,
    tags, aliases, headings, block ids, excerpts) may reach B through the
    detail, export, list or search, and a never-published page A created must
    not name itself in the chat's stored title or summary either."""

    with page_service.session_factory() as session:
        workspace_id = session.scalar(
            select(Workspace).where(Workspace.slug == "default")
        ).id
    published = page_service.write_page(
        workspace_slug="default",
        slug="shared-published-page",
        title="Shared Title",
        body_md="# Shared Title\n\nShared body.\n",
        actor_kind="human",
        actor_id=str(uuid4()),
        actor_provenance="browser_session",
    )
    author_id = uuid4()
    author = mcp_default_actor(
        scopes=("*",), actor_id=str(author_id), user_id=author_id
    )
    token = _call(page_service, author, "guide", {"workspace_id": str(workspace_id)})[
        "envelope"
    ]["conversation_token"]

    def call(name, arguments):
        return _call(
            page_service,
            author,
            name,
            {
                "workspace_id": str(workspace_id),
                "conversation_token": token,
                **arguments,
            },
        )

    begun = call(
        "begin_context_edit",
        {"page_id": str(published.id), "idempotency_key": f"b-{uuid4()}"},
    )
    assert begun["envelope"]["result_status"] == "succeeded", begun
    editing = begun["data"]
    private_body = (
        "# Shared Title\n\nShared body.\n\n## Private Heading\n\nPrivate paragraph.\n"
    )
    update_arguments = {
        "page_id": str(published.id),
        "title": "PRIVATE TITLE",
        "tags": ["private-tag"],
        "aliases": ["private-alias"],
        "body_md": private_body,
        "base_version_id": editing["version_id"],
        "base_content_hash": editing["content_hash"],
    }
    updated = call(
        "update_context_edit",
        {
            **update_arguments,
            "expected_editing_revision": editing["editing_revision"],
            "idempotency_key": f"u-{uuid4()}",
        },
    )
    assert updated["envelope"]["result_status"] == "succeeded", updated
    stale = call(
        "update_context_edit",
        {
            **update_arguments,
            "expected_editing_revision": 1,
            "idempotency_key": f"u-{uuid4()}",
        },
    )
    assert stale["envelope"]["result_status"] != "succeeded", stale
    patched = call(
        "patch_context_edit",
        {
            "page_id": str(published.id),
            "version_id": editing["version_id"],
            "expected_editing_revision": updated["data"]["editing_revision"],
            "idempotency_key": f"p-{uuid4()}",
            "operations": [
                {
                    "op": "replace_text",
                    "old_text": "Private paragraph.",
                    "new_text": "PRIVATE PATCH TEXT",
                    "at": {
                        "by": "heading_path",
                        "heading_path": ["Shared Title", "Private Heading"],
                    },
                }
            ],
        },
    )
    assert patched["envelope"]["result_status"] == "succeeded", patched
    created = call(
        "create_context_page",
        {"title": "PRIVATE NEW PAGE", "body_md": "# PRIVATE NEW PAGE\n\nbody\n"},
    )
    assert created["envelope"]["result_status"] == "succeeded", created

    service = McpChatService()
    reader = _reader(uuid4(), workspace_id, "editor")
    with page_service.session_factory() as session:
        chat = session.scalar(
            select(McpChatSession).where(
                McpChatSession.workspace_id == workspace_id,
                McpChatSession.mcp_session_id == token,
            )
        )
        stored_labels = (chat.title or "", chat.summary or "")
        surfaces = [
            service.read_chat(
                session=session,
                workspace_id=workspace_id,
                chat_session_id=chat.id,
                actor=reader,
            ),
            service.export_chats(
                session=session, workspace_id=workspace_id, actor=reader
            ),
            service.list_chats(
                session=session, workspace_id=workspace_id, actor=reader
            ),
            *(
                service.list_chats(
                    session=session,
                    workspace_id=workspace_id,
                    actor=reader,
                    q=needle,
                )
                for needle in ("PRIVATE TITLE", "Private Heading", "private-tag")
            ),
        ]
        author_view = service.read_chat(
            session=session,
            workspace_id=workspace_id,
            chat_session_id=chat.id,
            actor=_reader(author_id, workspace_id, "editor"),
        )

    author_json = author_view.model_dump_json()
    for secret in ("PRIVATE TITLE", "private-tag", "private-alias", "Private Heading"):
        assert secret in author_json, secret
    block_ids = {
        block.id
        for row in author_view.timeline
        if row.result_detail and row.result_detail.edit_receipt
        for block in row.result_detail.edit_receipt.changed_blocks
    }
    assert block_ids, "the author's receipts name the blocks the patch changed"

    for surface in surfaces:
        serialized = surface.model_dump_json()
        for secret in (*PRIVATE_STRINGS, *block_ids):
            assert secret not in serialized, secret
    for label in stored_labels:
        assert "PRIVATE" not in label, label
    # B still sees that the published page was edited, and how much.
    detail = surfaces[0]
    patch_row = next(
        row
        for row in detail.timeline
        if row.tool_name == "patch_context_edit" and row.result_status == "succeeded"
    )
    assert patch_row.redaction_reason is None
    assert patch_row.headline == "Edited: Shared Title"
    assert patch_row.edit_summary.operation_counts == {"replace_text": 1}
    assert patch_row.edit_summary.previews_withheld is True

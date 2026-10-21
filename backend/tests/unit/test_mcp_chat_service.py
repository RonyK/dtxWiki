import os
import time
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from sqlalchemy import event, func, select, update

from backend.mcp.conversation import resolve_conversation_token
from backend.models.agent import AgentPage
from backend.models.api_token import ApiToken
from backend.models.audit import AuditEvent
from backend.models.context import ContextClaim, ContextPage, ContextPageVersion
from backend.models.context_media import ContextMediaAsset
from backend.models.library import LibraryArtifact, LibraryItem, LibraryItemVersion
from backend.models.mcp_chat import (
    McpChatArtifactKind,
    McpChatArtifactLink,
    McpChatArtifactVerb,
    McpChatSession,
    McpChatToolCallArtifact,
)
from backend.models.mcp_trace import MCPToolCall
from backend.models.media import (
    BlobIncarnationRow,
    StoredBlobInspectionRow,
    StoredBlobRow,
    WorkspaceMediaWriteIntent,
)
from backend.models.user import User
from backend.models.workspace import MCPClient, Workspace
from backend.policy.auth import ActorContext
from backend.services.mcp_chat_service import (
    McpChatService,
    _as_utc,
    _is_legacy_count_title,
)
from backend.services.mcp_trace_capture import derive_artifact_links
from backend.services.read_errors import ReadNotFoundError


def test_chat_auto_title_fits_multicodepoint_subject_without_trailing_space(
    session_factory,
):
    service = McpChatService()
    with session_factory.begin() as session:
        workspace = session.scalar(select(Workspace).where(Workspace.slug == "default"))
        client = session.scalar(
            select(MCPClient).where(MCPClient.workspace_id == workspace.id)
        )
        subject = " ".join(["न्ते"] * 50)
        page = ContextPage(
            workspace_id=workspace.id,
            slug="multicodepoint-subject",
            title=subject,
            page_type="overview",
            lifecycle_status="published",
        )
        session.add(page)
        session.flush()
        version = ContextPageVersion(
            workspace_id=workspace.id,
            page_id=page.id,
            version_no=1,
            body_md="Body",
            content_hash="published-subject",
            actor_type="human",
            created_by_actor_kind="human",
            created_by_actor_id=str(uuid4()),
            version_status="published_snapshot",
        )
        session.add(version)
        session.flush()
        page.current_version_id = page.current_published_version_id = version.id
        call = _tool_call(
            client_id=client.id,
            workspace_id=workspace.id,
            mcp_session_id=uuid4(),
            tool_name="cite",
            request_id=uuid4(),
        )
        session.add(call)
        session.flush()
        chat = service.upsert_session_from_tool_call(
            session=session,
            workspace_id=workspace.id,
            client_id=client.id,
            mcp_session_id=str(call.mcp_session_id),
            tool_call=call,
        )
        service.link_artifact(
            session=session,
            chat_session=chat,
            tool_call=call,
            resource_kind=McpChatArtifactKind.CONTEXT_PAGE,
            resource_id=page.id,
            verb=McpChatArtifactVerb.CREATED,
        )
        session.flush()
        assert 1 <= len(chat.title) <= 120
        assert chat.title == chat.title.strip()
        assert subject.startswith(chat.title)
        assert chat.title.startswith("न्ते न्ते")


def test_mcp_chat_service_upserts_and_rolls_up_artifact_links(session_factory):
    service = McpChatService()
    with session_factory.begin() as session:
        workspace = session.scalar(select(Workspace).where(Workspace.slug == "default"))
        client = session.scalar(
            select(MCPClient).where(MCPClient.workspace_id == workspace.id)
        )
        page = ContextPage(
            workspace_id=workspace.id,
            slug="mcp-linked-page",
            title="MCP Linked Page",
            page_type="overview",
        )
        session.add(page)
        session.flush()
        first_call = _tool_call(
            client_id=client.id,
            workspace_id=workspace.id,
            mcp_session_id=uuid4(),
            tool_name="cite",
            request_id=uuid4(),
        )
        second_call = _tool_call(
            client_id=client.id,
            workspace_id=workspace.id,
            mcp_session_id=first_call.mcp_session_id,
            tool_name="cite",
            request_id=uuid4(),
        )
        session.add_all([first_call, second_call])
        session.flush()

        chat = service.upsert_session_from_tool_call(
            session=session,
            workspace_id=workspace.id,
            client_id=client.id,
            mcp_session_id=str(first_call.mcp_session_id),
            tool_call=first_call,
        )
        service.upsert_session_from_tool_call(
            session=session,
            workspace_id=workspace.id,
            client_id=client.id,
            mcp_session_id=str(first_call.mcp_session_id),
            tool_call=second_call,
        )
        service.link_artifact(
            session=session,
            chat_session=chat,
            tool_call=first_call,
            resource_kind=McpChatArtifactKind.CONTEXT_PAGE,
            resource_id=page.id,
            verb=McpChatArtifactVerb.CREATED,
        )
        service.link_artifact(
            session=session,
            chat_session=chat,
            tool_call=second_call,
            resource_kind=McpChatArtifactKind.CONTEXT_PAGE,
            resource_id=page.id,
            verb=McpChatArtifactVerb.CREATED,
        )
        session.flush()

        detail = service.read_chat(
            session=session,
            workspace_id=workspace.id,
            chat_session_id=chat.id,
        )

    assert detail.tool_call_count == 2
    assert detail.write_count == 2
    assert detail.linked_artifacts[0].artifact_title == "MCP Linked Page"
    assert detail.linked_artifacts[0].navigation_path == f"/context?target={page.id}"
    assert detail.linked_artifacts[0].occurrence_count == 2
    assert detail.title_source == "artifact"
    assert detail.action == "create"
    assert detail.subject == "MCP Linked Page"
    assert detail.subject_kind == "context_page"
    assert detail.primary_tool_name == "cite"
    assert len(detail.timeline) == 2
    for timeline_item in detail.timeline:
        assert timeline_item.input_display_items == []
        assert [item.key for item in timeline_item.result_display_items] == [
            "created_artifact"
        ]
        assert timeline_item.result_display_items[0].text == "MCP Linked Page"
    # Auto-title is a concise anchor, not the old
    # "<client> · <N> actions · <anchor>" dump (Sprint-6 design fix).
    assert "·" not in detail.title
    assert "actions" not in detail.title
    # Summary is now a semantic action (verb + artifact title), not a count
    # restatement that duplicates the call/write chips (item 4).
    assert detail.summary == "Created: MCP Linked Page"


def test_read_chat_redacts_hidden_artifacts_for_gated_reader(session_factory):
    # Codex round-12: chat history stores artifact ids captured under the acting
    # client's permissions. A later gated reader must not recover an archived
    # context page or restricted library item — id, title, and path all drop.
    from backend.models.library import LibraryItem

    service = McpChatService()
    with session_factory.begin() as session:
        workspace = session.scalar(select(Workspace).where(Workspace.slug == "default"))
        client = session.scalar(
            select(MCPClient).where(MCPClient.workspace_id == workspace.id)
        )
        page = ContextPage(
            workspace_id=workspace.id,
            slug="mcp-archived",
            title="Secret Archived Page",
            page_type="overview",
            lifecycle_status="archived",
        )
        lib = LibraryItem(
            workspace_id=workspace.id,
            connector_type="upload",
            format_hint="pdf",
            uri="https://example.com/mcp-restricted.pdf",
            title="Restricted Source",
            trust_level="restricted",
            lifecycle_status="registered",
        )
        session.add_all([page, lib])
        session.flush()
        page_id, lib_id = page.id, lib.id
        call = _tool_call(
            client_id=client.id,
            workspace_id=workspace.id,
            mcp_session_id=uuid4(),
            tool_name="write_draft",
            request_id=uuid4(),
        )
        session.add(call)
        session.flush()
        chat = service.upsert_session_from_tool_call(
            session=session,
            workspace_id=workspace.id,
            client_id=client.id,
            mcp_session_id=str(call.mcp_session_id),
            tool_call=call,
        )
        service.link_artifact(
            session=session,
            chat_session=chat,
            tool_call=call,
            resource_kind=McpChatArtifactKind.CONTEXT_PAGE,
            resource_id=page_id,
            verb=McpChatArtifactVerb.CREATED,
        )
        service.link_artifact(
            session=session,
            chat_session=chat,
            tool_call=call,
            resource_kind=McpChatArtifactKind.LIBRARY_ITEM,
            resource_id=lib_id,
            verb=McpChatArtifactVerb.CITED,
        )
        session.flush()

        viewer = ActorContext(
            actor_kind="human",
            actor_id="viewer",
            token_kind="session",
            scopes=("read",),
            workspace_id=workspace.id,
            workspace_role="viewer",
            role="viewer",
        )
        gated = service.read_chat(
            session=session,
            workspace_id=workspace.id,
            chat_session_id=chat.id,
            actor=viewer,
        )
        ungated = service.read_chat(
            session=session, workspace_id=workspace.id, chat_session_id=chat.id
        )

    # Gated viewer: both hidden artifacts drop entirely (no id/title/path leak).
    assert gated.linked_artifacts == []
    gated_ids = {
        artifact.resource_id
        for entry in gated.timeline
        for artifact in entry.linked_artifacts
    }
    assert page_id not in gated_ids
    assert lib_id not in gated_ids
    # The persisted auto title/summary (derived from the hidden page at write
    # time) must be re-derived, not replayed.
    assert "Secret Archived Page" not in (gated.title or "")
    assert "Secret Archived Page" not in (gated.summary or "")
    assert gated.subject is None
    # A call that touched a hidden target has its raw input/result redacted.
    for entry in gated.timeline:
        assert entry.input_summary == {}
        assert entry.result_summary is None
        assert entry.subject is None
    # Ungated (system) read still resolves everything.
    ungated_titles = {a.artifact_title for a in ungated.linked_artifacts}
    assert "Secret Archived Page" in ungated_titles
    assert "Restricted Source" in ungated_titles
    assert ungated.timeline[0].input_summary == {"query": "citation policy"}


def test_actor_bound_detail_and_export_reproject_frozen_summary(session_factory):
    service = McpChatService()
    hidden_title = "Frozen Restricted Acquisition Plan"
    with session_factory.begin() as session:
        workspace = session.scalar(select(Workspace).where(Workspace.slug == "default"))
        client = session.scalar(
            select(MCPClient).where(MCPClient.workspace_id == workspace.id)
        )
        owner = User(
            email="frozen-chat-owner@example.com",
            password_hash="test-hash",
            display_name="Frozen Chat Owner",
        )
        hidden_page = ContextPage(
            workspace_id=workspace.id,
            slug="frozen-restricted-plan",
            title=hidden_title,
            page_type="decision",
            lifecycle_status="archived",
        )
        session.add_all([owner, hidden_page])
        session.flush()
        call = _tool_call(
            client_id=client.id,
            workspace_id=workspace.id,
            mcp_session_id=uuid4(),
            tool_name="write_draft",
            request_id=uuid4(),
            input_summary={"title": hidden_title, "reason": "sensitive authoring"},
        )
        session.add(call)
        session.flush()
        chat = service.upsert_session_from_tool_call(
            session=session,
            workspace_id=workspace.id,
            client_id=client.id,
            mcp_session_id=str(call.mcp_session_id),
            tool_call=call,
            owner_user_id=owner.id,
        )
        service.link_artifact(
            session=session,
            chat_session=chat,
            tool_call=call,
            resource_kind=McpChatArtifactKind.CONTEXT_PAGE,
            resource_id=hidden_page.id,
            verb=McpChatArtifactVerb.CREATED,
        )
        assert hidden_title in (chat.summary or "")
        chat.last_activity_at = datetime(2020, 1, 1, tzinfo=UTC)
        service.freeze_summary_if_ended(chat)
        assert hidden_title in (chat.frozen_summary or "")
        session.flush()

        reader_id = uuid4()
        reader = ActorContext(
            actor_kind="human",
            actor_id=str(reader_id),
            user_id=reader_id,
            token_kind="session",
            scopes=("read",),
            workspace_id=workspace.id,
            workspace_role="editor",
            role="editor",
        )
        gated = service.read_chat(
            session=session,
            workspace_id=workspace.id,
            chat_session_id=chat.id,
            actor=reader,
        )
        exported = service.export_chats(
            session=session,
            workspace_id=workspace.id,
            actor=reader,
        )
        ungated = service.read_chat(
            session=session,
            workspace_id=workspace.id,
            chat_session_id=chat.id,
        )

    exported_chat = next(item for item in exported.chats if item.id == gated.id)
    assert gated.frozen_at is not None
    assert gated.frozen_summary == gated.summary
    assert exported_chat.frozen_summary == exported_chat.summary
    assert hidden_title not in (gated.frozen_summary or "")
    assert hidden_title not in (exported_chat.frozen_summary or "")
    # Internal/system reads preserve the historical frozen audit value.
    assert hidden_title in (ungated.frozen_summary or "")


def test_artifact_links_paginate_only_actor_visible_targets(session_factory):
    service = McpChatService()
    base = datetime(2026, 8, 5, 6, 0, tzinfo=UTC)
    with session_factory.begin() as session:
        workspace = session.scalar(select(Workspace).where(Workspace.slug == "default"))
        client = session.scalar(
            select(MCPClient).where(MCPClient.workspace_id == workspace.id)
        )
        visible_item = LibraryItem(
            workspace_id=workspace.id,
            connector_type="upload",
            format_hint="md",
            uri="https://example.com/visible-pagination.md",
            title="Visible Pagination Source",
            trust_level="unknown",
            lifecycle_status="registered",
        )
        hidden_items = [
            LibraryItem(
                workspace_id=workspace.id,
                connector_type="upload",
                format_hint="pdf",
                uri=f"https://example.com/hidden-pagination-{index}.pdf",
                title=f"Hidden Pagination Source {index}",
                trust_level="restricted",
                lifecycle_status="registered",
            )
            for index in range(2)
        ]
        session.add_all([visible_item, *hidden_items])
        session.flush()

        session_id = uuid4()
        chat = None
        for index, item in enumerate([visible_item, *hidden_items]):
            call = _tool_call(
                client_id=client.id,
                workspace_id=workspace.id,
                mcp_session_id=session_id,
                tool_name="read",
                request_id=uuid4(),
                input_summary={"target_id": str(item.id)},
            )
            call.started_at = base.replace(minute=index)
            call.completed_at = base.replace(minute=index)
            session.add(call)
            session.flush()
            chat = service.upsert_session_from_tool_call(
                session=session,
                workspace_id=workspace.id,
                client_id=client.id,
                mcp_session_id=str(session_id),
                tool_call=call,
            )
            service.link_artifact(
                session=session,
                chat_session=chat,
                tool_call=call,
                resource_kind=McpChatArtifactKind.LIBRARY_ITEM,
                resource_id=item.id,
                verb=(
                    McpChatArtifactVerb.REFERENCED
                    if item is visible_item
                    else McpChatArtifactVerb.EDITED
                ),
            )
        assert chat is not None

        reader_id = uuid4()
        reader = ActorContext(
            actor_kind="human",
            actor_id=str(reader_id),
            user_id=reader_id,
            token_kind="session",
            scopes=("read",),
            workspace_id=workspace.id,
            workspace_role="editor",
            role="editor",
        )
        visible_page = service.list_artifact_links(
            session=session,
            workspace_id=workspace.id,
            actor=reader,
            limit=1,
        )
        hidden_hit = service.list_artifact_links(
            session=session,
            workspace_id=workspace.id,
            actor=reader,
            verb=McpChatArtifactVerb.EDITED.value,
            limit=1,
        )
        empty_miss = service.list_artifact_links(
            session=session,
            workspace_id=workspace.id,
            actor=reader,
            verb=McpChatArtifactVerb.CITED.value,
            limit=1,
        )
        raw_page = service.list_artifact_links(
            session=session,
            workspace_id=workspace.id,
            limit=1,
        )

    assert [item.resource_id for item in visible_page.items] == [visible_item.id]
    assert visible_page.next_cursor is None
    assert hidden_hit.items == empty_miss.items == []
    assert hidden_hit.next_cursor is None
    assert empty_miss.next_cursor is None
    assert raw_page.items[0].resource_id in {item.id for item in hidden_items}
    assert raw_page.next_cursor == "1"


def test_artifact_link_sql_visibility_matches_canonical_actor_policy(session_factory):
    service = McpChatService()
    with session_factory.begin() as session:
        workspace = session.scalar(select(Workspace).where(Workspace.slug == "default"))
        client = session.scalar(
            select(MCPClient).where(MCPClient.workspace_id == workspace.id)
        )
        other_workspace = Workspace(
            slug="artifact-link-policy-other",
            title="Artifact link policy other",
            name="Artifact link policy other",
        )
        visible_context = ContextPage(
            workspace_id=workspace.id,
            slug="artifact-link-visible-context",
            title="Visible Context",
            page_type="overview",
        )
        archived_context = ContextPage(
            workspace_id=workspace.id,
            slug="artifact-link-archived-context",
            title="Archived Context",
            page_type="overview",
            lifecycle_status="archived",
        )
        visible_agent = AgentPage(
            workspace_id=workspace.id,
            category="persona",
            title="Visible Agent",
            slug="artifact-link-visible-agent",
            lifecycle_status="active",
            sensitivity="workspace",
        )
        owner_agent = AgentPage(
            workspace_id=workspace.id,
            category="memory",
            title="Owner Agent",
            slug="artifact-link-owner-agent",
            lifecycle_status="active",
            sensitivity="private",
        )
        visible_library = LibraryItem(
            workspace_id=workspace.id,
            connector_type="upload",
            format_hint="md",
            uri="https://example.com/policy-visible.md",
            title="Visible Library",
            trust_level="unknown",
            lifecycle_status="registered",
        )
        restricted_library = LibraryItem(
            workspace_id=workspace.id,
            connector_type="upload",
            format_hint="pdf",
            uri="https://example.com/policy-restricted.pdf",
            title="Restricted Library",
            trust_level="restricted",
            lifecycle_status="registered",
        )
        foreign_library = LibraryItem(
            workspace_id=other_workspace.id,
            connector_type="upload",
            format_hint="md",
            uri="https://example.com/policy-foreign.md",
            title="Foreign Library",
            trust_level="unknown",
            lifecycle_status="registered",
        )
        session.add_all(
            [
                other_workspace,
                visible_context,
                archived_context,
                visible_agent,
                owner_agent,
                visible_library,
                restricted_library,
            ]
        )
        session.flush()
        foreign_library.workspace_id = other_workspace.id
        session.add(foreign_library)
        context_version = ContextPageVersion(
            workspace_id=visible_context.workspace_id,
            page_id=visible_context.id,
            version_no=1,
            body_md="# Visible Context",
            content_hash="artifact-link-visible-context-v1",
            actor_type="human",
            created_by_actor_kind="human",
            created_by_actor_id="tester",
            version_status="draft",
        )
        session.add(context_version)
        session.flush()
        visible_context.latest_draft_version_id = context_version.id

        call = _tool_call(
            client_id=client.id,
            workspace_id=workspace.id,
            mcp_session_id=uuid4(),
            tool_name="read",
            request_id=uuid4(),
        )
        session.add(call)
        session.flush()
        chat = service.upsert_session_from_tool_call(
            session=session,
            workspace_id=workspace.id,
            client_id=client.id,
            mcp_session_id=str(call.mcp_session_id),
            tool_call=call,
        )
        linked = (
            (McpChatArtifactKind.CONTEXT_PAGE, visible_context.id),
            (McpChatArtifactKind.CONTEXT_PAGE, archived_context.id),
            (McpChatArtifactKind.AGENT_PAGE, visible_agent.id),
            (McpChatArtifactKind.AGENT_PAGE, owner_agent.id),
            (McpChatArtifactKind.LIBRARY_ITEM, visible_library.id),
            (McpChatArtifactKind.LIBRARY_ITEM, restricted_library.id),
            (McpChatArtifactKind.LIBRARY_ITEM, foreign_library.id),
        )
        for resource_kind, resource_id in linked:
            service.link_artifact(
                session=session,
                chat_session=chat,
                tool_call=call,
                resource_kind=resource_kind,
                resource_id=resource_id,
                verb=McpChatArtifactVerb.REFERENCED,
            )

        editor_id = uuid4()
        editor = ActorContext(
            actor_kind="human",
            actor_id=str(editor_id),
            user_id=editor_id,
            token_kind="session",
            scopes=("read",),
            workspace_id=workspace.id,
            workspace_role="editor",
            role="editor",
        )
        owner_id = uuid4()
        owner = ActorContext(
            actor_kind="human",
            actor_id=str(owner_id),
            user_id=owner_id,
            token_kind="session",
            scopes=("read", "admin"),
            workspace_id=workspace.id,
            workspace_role="owner",
            role="owner",
        )
        editor_page = service.list_artifact_links(
            session=session,
            workspace_id=workspace.id,
            actor=editor,
        )
        owner_page = service.list_artifact_links(
            session=session,
            workspace_id=workspace.id,
            actor=owner,
        )

    assert {item.resource_id for item in editor_page.items} == {
        visible_context.id,
        visible_agent.id,
        visible_library.id,
    }
    assert {item.resource_id for item in owner_page.items} == {
        visible_context.id,
        visible_agent.id,
        owner_agent.id,
        visible_library.id,
        restricted_library.id,
    }
    for page in (editor_page, owner_page):
        assert archived_context.id not in {item.resource_id for item in page.items}
        assert foreign_library.id not in {item.resource_id for item in page.items}
        assert page.next_cursor is None


def _seed_media_blob(session, workspace_id: UUID, *, salt: str) -> tuple[UUID, UUID]:
    """Minimal blob/inspection plumbing a ContextMediaAsset row's FKs require.

    Mirrors backend/tests/integration/test_context_media_repository.py's
    _seed_shared_image, trimmed to what this file's visibility test needs.
    Phase 5c (context_media_orphan_sweep) owns that module; this is a
    standalone copy so this file does not import a test module from another
    phase's test file.
    """

    now = datetime(2026, 8, 29, 0, 0, tzinfo=UTC)
    if session.get(WorkspaceMediaWriteIntent, workspace_id) is None:
        session.add(
            WorkspaceMediaWriteIntent(
                workspace_id=workspace_id,
                state="open",
                delete_generation=0,
                updated_at=now,
            )
        )
        session.flush()
    blob_id = uuid4()
    inspection_id = uuid4()
    session.add(
        StoredBlobRow(
            workspace_id=workspace_id, id=blob_id, sha256=salt * 64, next_epoch=2
        )
    )
    session.flush()
    session.add(
        BlobIncarnationRow(
            workspace_id=workspace_id,
            blob_id=blob_id,
            blob_epoch=1,
            byte_size=68,
            storage_key=f"w/{workspace_id.hex}/{salt}/{blob_id.hex}",
            state="available",
            publish_generation=1,
            security_generation=0,
            gc_generation=0,
            available_at=now,
        )
    )
    session.flush()
    session.add(
        StoredBlobInspectionRow(
            workspace_id=workspace_id,
            id=inspection_id,
            blob_id=blob_id,
            blob_epoch=1,
            registry_version="registry-v1",
            detector_id="png-magic",
            detector_version="1",
            format_id="png",
            canonical_media_type="image/png",
            validation_schema_version="png-v1",
            metadata_json={},
            scanner_verdict="not_configured",
            scanner_provider=None,
            scanner_version=None,
            ordinary_available=True,
            created_at=now,
        )
    )
    session.flush()
    return blob_id, inspection_id


def test_media_asset_link_sql_predicate_matches_python_gate(session_factory):
    """A page-bound media asset is visible exactly when BOTH its page is
    visible to the actor AND the specific version it was created for is not
    someone else's private, unshared "editing" row (P3.1/P3.2: every asset
    is page-owned now -- there is no more page-less "session" ownership
    shape -- but a page having some visible head does not make every asset
    registered against it visible; page-first creation and begin_editing
    can register an asset against a private editing row on a page that
    separately has its own visible head).

    _actor_visible_artifact_link_predicate's docstring calls itself
    "equivalent to _artifact_payload gates" -- a stated invariant, not a
    coincidence of the combined list_artifact_links() pipeline. This test
    checks all three: the end-to-end visible set, the SQL predicate run
    directly, and the Python per-row gate run directly, on the SAME fixture.
    """

    service = McpChatService()
    with session_factory.begin() as session:
        workspace = session.scalar(select(Workspace).where(Workspace.slug == "default"))
        client = session.scalar(
            select(MCPClient).where(MCPClient.workspace_id == workspace.id)
        )
        blob_id, inspection_id = _seed_media_blob(session, workspace.id, salt="c")

        visible_page = ContextPage(
            workspace_id=workspace.id,
            slug="media-link-visible-page",
            title="Media Link Visible Page",
            page_type="overview",
        )
        archived_page = ContextPage(
            workspace_id=workspace.id,
            slug="media-link-archived-page",
            title="Media Link Archived Page",
            page_type="overview",
            lifecycle_status="archived",
        )
        session.add_all([visible_page, archived_page])
        session.flush()
        visible_version = ContextPageVersion(
            workspace_id=visible_page.workspace_id,
            page_id=visible_page.id,
            version_no=1,
            body_md="# Media Link Visible Page",
            content_hash="media-link-visible-page-v1",
            actor_type="human",
            created_by_actor_kind="human",
            created_by_actor_id="tester",
            version_status="draft",
        )
        session.add(visible_version)
        session.flush()
        visible_page.latest_draft_version_id = visible_version.id

        reader_id = uuid4()
        reader = ActorContext(
            actor_kind="human",
            actor_id=str(reader_id),
            user_id=reader_id,
            token_kind="session",
            scopes=("read",),
            workspace_id=workspace.id,
            workspace_role="editor",
            role="editor",
        )
        other_actor_id = uuid4()

        # Two private "editing" rows on the SAME visible page, one per
        # author -- the exact pointer shape that only became reachable once
        # page-first creation/begin_editing existed (a never-submitted
        # editing row coexisting with the page's own separately-visible
        # draft head).
        reader_editing_version = ContextPageVersion(
            workspace_id=visible_page.workspace_id,
            page_id=visible_page.id,
            version_no=2,
            body_md="# Reader's private edit",
            content_hash="media-link-reader-editing-v1",
            actor_type="human",
            created_by_actor_kind="human",
            created_by_actor_id=str(reader_id),
            version_status="editing",
        )
        foreign_editing_version = ContextPageVersion(
            workspace_id=visible_page.workspace_id,
            page_id=visible_page.id,
            version_no=3,
            body_md="# Someone else's private edit",
            content_hash="media-link-foreign-editing-v1",
            actor_type="human",
            created_by_actor_kind="human",
            created_by_actor_id=str(other_actor_id),
            version_status="editing",
        )
        session.add_all([reader_editing_version, foreign_editing_version])
        session.flush()

        def _asset(*, page_id, created_for_version_id, created_by_actor_id):
            return ContextMediaAsset(
                workspace_id=workspace.id,
                id=uuid4(),
                page_id=page_id,
                created_for_version_id=created_for_version_id,
                blob_id=blob_id,
                blob_epoch=1,
                inspection_id=inspection_id,
                kind="image",
                original_filename="diagram.png",
                canonical_media_type="image/png",
                logical_bytes=68,
                policy_revision="context-mvp-v1",
                association_state="active",
                created_by_actor_kind="human",
                created_by_actor_id=created_by_actor_id,
            )

        page_visible_asset = _asset(
            page_id=visible_page.id,
            created_for_version_id=visible_version.id,
            created_by_actor_id="uploader",
        )
        page_hidden_asset = _asset(
            page_id=archived_page.id,
            created_for_version_id=visible_version.id,
            created_by_actor_id="uploader",
        )
        # Tombstoned but page still visible: excluded anyway. A tombstoned
        # asset is queued for physical blob deletion, so its content may
        # already be gone -- current state gates provenance visibility here
        # exactly like an archived page does, not raw upload history.
        tombstoned_asset = _asset(
            page_id=visible_page.id,
            created_for_version_id=visible_version.id,
            created_by_actor_id="uploader",
        )
        tombstoned_asset.association_state = "tombstoned"
        # Own private editing upload: visible to the reader even though it
        # is not yet part of any shared/published content.
        owned_editing_asset = _asset(
            page_id=visible_page.id,
            created_for_version_id=reader_editing_version.id,
            created_by_actor_id=str(reader_id),
        )
        # A different author's private editing upload, on the SAME visible
        # page: this is exactly the gap the page-visible-at-all check alone
        # would miss.
        foreign_editing_asset = _asset(
            page_id=visible_page.id,
            created_for_version_id=foreign_editing_version.id,
            created_by_actor_id=str(other_actor_id),
        )
        session.add_all(
            [
                page_visible_asset,
                page_hidden_asset,
                tombstoned_asset,
                owned_editing_asset,
                foreign_editing_asset,
            ]
        )
        session.flush()

        call = _tool_call(
            client_id=client.id,
            workspace_id=workspace.id,
            mcp_session_id=uuid4(),
            tool_name="upload_image",
            request_id=uuid4(),
        )
        session.add(call)
        session.flush()
        chat = service.upsert_session_from_tool_call(
            session=session,
            workspace_id=workspace.id,
            client_id=client.id,
            mcp_session_id=str(call.mcp_session_id),
            tool_call=call,
        )
        linked = (
            page_visible_asset.id,
            page_hidden_asset.id,
            tombstoned_asset.id,
            owned_editing_asset.id,
            foreign_editing_asset.id,
        )
        for resource_id in linked:
            service.link_artifact(
                session=session,
                chat_session=chat,
                tool_call=call,
                resource_kind=McpChatArtifactKind.CONTEXT_MEDIA_ASSET,
                resource_id=resource_id,
                verb=McpChatArtifactVerb.CREATED,
            )

        # 1) End-to-end: the actual read path a gated reader hits.
        listed = service.list_artifact_links(
            session=session, workspace_id=workspace.id, actor=reader
        )
        listed_ids = {item.resource_id for item in listed.items}

        # 2) The SQL predicate alone, run directly against the link rows.
        predicate = service._actor_visible_artifact_link_predicate(reader, workspace.id)
        sql_visible_ids = set(
            session.scalars(
                select(McpChatArtifactLink.resource_id).where(
                    McpChatArtifactLink.chat_session_id == chat.id,
                    McpChatArtifactLink.resource_kind
                    == McpChatArtifactKind.CONTEXT_MEDIA_ASSET.value,
                    predicate,
                )
            ).all()
        )

        # 3) The Python per-row gate alone, on the SAME rows.
        links = session.scalars(
            select(McpChatArtifactLink).where(
                McpChatArtifactLink.chat_session_id == chat.id,
                McpChatArtifactLink.resource_kind
                == McpChatArtifactKind.CONTEXT_MEDIA_ASSET.value,
            )
        ).all()
        python_visible_ids = {
            link.resource_id
            for link in links
            if service._artifact_payload(session, link, reader, workspace.id)
            is not None
        }
        listed_titles = {item.resource_id: item.artifact_title for item in listed.items}
        listed_paths = {item.resource_id: item.navigation_path for item in listed.items}

    expected = {page_visible_asset.id, owned_editing_asset.id}
    assert listed_ids == expected
    assert sql_visible_ids == expected
    assert python_visible_ids == expected
    assert listed_titles[page_visible_asset.id] == "diagram.png"
    assert listed_paths[page_visible_asset.id] == f"/context?target={visible_page.id}"
    assert listed_titles[owned_editing_asset.id] == "diagram.png"
    assert listed_paths[owned_editing_asset.id] == f"/context?target={visible_page.id}"


def test_media_asset_title_lookup_is_workspace_scoped_for_colliding_ids(
    session_factory,
):
    """ContextMediaAsset's primary key is the COMPOSITE (workspace_id, id) --
    id alone is not unique across workspaces (its three UniqueConstraints are
    all workspace-scoped), and it is a caller-supplied value
    (UploadImageInput.marker_uuid, passed straight through as asset_id in
    context_image_ingest.py). So an editor in workspace B can upload with a
    marker_uuid that collides with an existing asset id in workspace A. A
    lookup that filters on id alone (`WHERE id = :resource_id`, no workspace
    scope) would then match rows in BOTH workspaces and raise
    MultipleResultsFound instead of resolving A's own row -- turning a
    cross-workspace collision into a 500 for A's chat listing. This pins that
    the composite key is looked up as a unit, not checked after the fact.
    """

    service = McpChatService()
    with session_factory.begin() as session:
        workspace_a = session.scalar(
            select(Workspace).where(Workspace.slug == "default")
        )
        workspace_b = Workspace(
            slug="media-collision-b",
            title="Media Collision B",
            name="Media Collision B",
        )
        session.add(workspace_b)
        session.flush()
        client_a = session.scalar(
            select(MCPClient).where(MCPClient.workspace_id == workspace_a.id)
        )

        blob_a, inspection_a = _seed_media_blob(session, workspace_a.id, salt="f")
        blob_b, inspection_b = _seed_media_blob(session, workspace_b.id, salt="1")

        page_a = ContextPage(
            workspace_id=workspace_a.id,
            slug="media-collision-page-a",
            title="Media Collision Page A",
            page_type="overview",
        )
        session.add(page_a)
        session.flush()
        version_a = ContextPageVersion(
            workspace_id=page_a.workspace_id,
            page_id=page_a.id,
            version_no=1,
            body_md="# Media Collision Page A",
            content_hash="media-collision-page-a-v1",
            actor_type="human",
            created_by_actor_kind="human",
            created_by_actor_id="tester",
            version_status="draft",
        )
        session.add(version_a)
        session.flush()
        page_a.latest_draft_version_id = version_a.id

        shared_id = uuid4()
        asset_a = ContextMediaAsset(
            workspace_id=workspace_a.id,
            id=shared_id,
            page_id=page_a.id,
            created_for_version_id=version_a.id,
            blob_id=blob_a,
            blob_epoch=1,
            inspection_id=inspection_a,
            kind="image",
            original_filename="a.png",
            canonical_media_type="image/png",
            logical_bytes=68,
            policy_revision="context-mvp-v1",
            association_state="active",
            created_by_actor_kind="human",
            created_by_actor_id="uploader-a",
        )
        # workspace B's colliding asset just needs ITS OWN page/version (P3.1:
        # every asset is page-owned now) -- the collision is entirely on `id`.
        page_b = ContextPage(
            workspace_id=workspace_b.id,
            slug="media-collision-page-b",
            title="Media Collision Page B",
            page_type="overview",
        )
        session.add(page_b)
        session.flush()
        version_b = ContextPageVersion(
            workspace_id=page_b.workspace_id,
            page_id=page_b.id,
            version_no=1,
            body_md="# Media Collision Page B",
            content_hash="media-collision-page-b-v1",
            actor_type="human",
            created_by_actor_kind="human",
            created_by_actor_id="uploader-b",
            version_status="draft",
        )
        session.add(version_b)
        session.flush()
        page_b.latest_draft_version_id = version_b.id
        asset_b = ContextMediaAsset(
            workspace_id=workspace_b.id,
            id=shared_id,
            page_id=page_b.id,
            created_for_version_id=version_b.id,
            blob_id=blob_b,
            blob_epoch=1,
            inspection_id=inspection_b,
            kind="image",
            original_filename="b.png",
            canonical_media_type="image/png",
            logical_bytes=68,
            policy_revision="context-mvp-v1",
            association_state="active",
            created_by_actor_kind="human",
            created_by_actor_id="uploader-b",
        )
        session.add_all([asset_a, asset_b])
        session.flush()

        call = _tool_call(
            client_id=client_a.id,
            workspace_id=workspace_a.id,
            mcp_session_id=uuid4(),
            tool_name="upload_image",
            request_id=uuid4(),
        )
        session.add(call)
        session.flush()
        chat = service.upsert_session_from_tool_call(
            session=session,
            workspace_id=workspace_a.id,
            client_id=client_a.id,
            mcp_session_id=str(call.mcp_session_id),
            tool_call=call,
        )
        link = service.link_artifact(
            session=session,
            chat_session=chat,
            tool_call=call,
            resource_kind=McpChatArtifactKind.CONTEXT_MEDIA_ASSET,
            resource_id=shared_id,
            verb=McpChatArtifactVerb.CREATED,
        )

        reader_id = uuid4()
        reader = ActorContext(
            actor_kind="human",
            actor_id=str(reader_id),
            user_id=reader_id,
            token_kind="session",
            scopes=("read",),
            workspace_id=workspace_a.id,
            workspace_role="editor",
            role="editor",
        )

        # Must resolve workspace A's own row (a.png / page_a) without
        # raising, not workspace B's colliding row.
        payload = service._artifact_payload(session, link, reader, workspace_a.id)

    assert payload is not None
    assert payload.artifact_title == "a.png"
    assert payload.navigation_path == f"/context?target={page_a.id}"


def test_timeline_attaches_artifact_to_every_call_that_touched_it(session_factory):
    """T9b-FU1: two tool calls editing the SAME page must BOTH carry the
    artifact and an "Edited: <title>" headline/schema_version in the per-call
    timeline. Pre-fix the rollup's last_mcp_tool_call_id attached the artifact
    to the latest call only, leaving the earlier call's row empty.
    """
    service = McpChatService()
    with session_factory.begin() as session:
        workspace = session.scalar(select(Workspace).where(Workspace.slug == "default"))
        client = session.scalar(
            select(MCPClient).where(MCPClient.workspace_id == workspace.id)
        )
        page = ContextPage(
            workspace_id=workspace.id,
            slug="multi-edit-page",
            title="Multi Edit Page",
            page_type="overview",
        )
        session.add(page)
        session.flush()
        session_uuid = uuid4()
        first_call = _tool_call(
            client_id=client.id,
            workspace_id=workspace.id,
            mcp_session_id=session_uuid,
            tool_name="update_draft",
            request_id=uuid4(),
        )
        second_call = _tool_call(
            client_id=client.id,
            workspace_id=workspace.id,
            mcp_session_id=session_uuid,
            tool_name="update_draft",
            request_id=uuid4(),
        )
        # A third call in the SAME session that does NOT touch the artifact —
        # it must stay unlinked (guards against the opposite regression: the
        # chat-level artifact being attached to every call).
        third_call = _tool_call(
            client_id=client.id,
            workspace_id=workspace.id,
            mcp_session_id=session_uuid,
            tool_name="search",
            request_id=uuid4(),
        )
        session.add_all([first_call, second_call, third_call])
        session.flush()
        chat = service.upsert_session_from_tool_call(
            session=session,
            workspace_id=workspace.id,
            client_id=client.id,
            mcp_session_id=str(session_uuid),
            tool_call=first_call,
        )
        for later in (second_call, third_call):
            service.upsert_session_from_tool_call(
                session=session,
                workspace_id=workspace.id,
                client_id=client.id,
                mcp_session_id=str(session_uuid),
                tool_call=later,
            )
        for call in (first_call, second_call):
            service.link_artifact(
                session=session,
                chat_session=chat,
                tool_call=call,
                resource_kind=McpChatArtifactKind.CONTEXT_PAGE,
                resource_id=page.id,
                verb=McpChatArtifactVerb.EDITED,
            )
        session.flush()

        detail = service.read_chat(
            session=session,
            workspace_id=workspace.id,
            chat_session_id=chat.id,
        )

    assert len(detail.timeline) == 3
    rows = {row.id: row for row in detail.timeline}
    # The two calls that edited the page carry the artifact + headline — not
    # only the latest call.
    for call in (first_call, second_call):
        row = rows[call.id]
        assert [a.resource_id for a in row.linked_artifacts] == [page.id]
        assert row.linked_artifacts[0].artifact_title == "Multi Edit Page"
        assert row.linked_artifacts[0].verb == "edited"
        assert row.linked_verbs == ["edited"]
        assert [item.key for item in row.input_display_items] == ["target_artifact"]
        assert [item.key for item in row.result_display_items] == ["updated_artifact"]
        assert row.headline == "Edited: Multi Edit Page"
        assert row.title_source == "artifact"
        assert row.action == "edit"
        assert row.subject == "Multi Edit Page"
        assert row.subject_kind == "context_page"
        assert row.primary_tool_name == "update_draft"
        assert row.schema_version == "llmwiki.mcp.v1"
    # The unlinked third call must NOT inherit the chat-level artifact/headline.
    unlinked = rows[third_call.id]
    assert unlinked.linked_artifacts == []
    assert unlinked.linked_verbs == []
    assert unlinked.headline != "Edited: Multi Edit Page"
    assert unlinked.title_source == "query"
    assert unlinked.action == "search"
    assert unlinked.subject == "citation policy"
    assert unlinked.subject_kind == "query"
    # The chat-level rollup still aggregates to a single link (occurrence 2).
    assert len(detail.linked_artifacts) == 1
    assert detail.linked_artifacts[0].occurrence_count == 2


def test_link_artifact_per_call_edge_is_idempotent(session_factory):
    """T9b-FU1: the same call re-reporting the same (artifact, verb) must not
    create duplicate per-call edges (unique key holds), so the timeline row
    lists the artifact exactly once."""
    service = McpChatService()
    with session_factory.begin() as session:
        workspace = session.scalar(select(Workspace).where(Workspace.slug == "default"))
        client = session.scalar(
            select(MCPClient).where(MCPClient.workspace_id == workspace.id)
        )
        page = ContextPage(
            workspace_id=workspace.id,
            slug="idempotent-edge-page",
            title="Idempotent Edge Page",
            page_type="overview",
        )
        session.add(page)
        session.flush()
        call = _tool_call(
            client_id=client.id,
            workspace_id=workspace.id,
            mcp_session_id=uuid4(),
            tool_name="write_draft",
            request_id=uuid4(),
        )
        session.add(call)
        session.flush()
        chat = service.upsert_session_from_tool_call(
            session=session,
            workspace_id=workspace.id,
            client_id=client.id,
            mcp_session_id=str(call.mcp_session_id),
            tool_call=call,
        )
        for _ in range(2):
            service.link_artifact(
                session=session,
                chat_session=chat,
                tool_call=call,
                resource_kind=McpChatArtifactKind.CONTEXT_PAGE,
                resource_id=page.id,
                verb=McpChatArtifactVerb.CREATED,
            )
        session.flush()
        edge_count = session.scalar(
            select(func.count())
            .select_from(McpChatToolCallArtifact)
            .where(McpChatToolCallArtifact.mcp_tool_call_id == call.id)
        )
        detail = service.read_chat(
            session=session,
            workspace_id=workspace.id,
            chat_session_id=chat.id,
        )

    assert edge_count == 1
    assert len(detail.timeline) == 1
    assert [a.resource_id for a in detail.timeline[0].linked_artifacts] == [page.id]


def test_link_artifact_rollup_keeps_latest_call_when_linked_out_of_order(
    session_factory,
):
    """Review follow-up (acece52 / 08bb502): tool calls can be persisted out of
    timestamp order, so BOTH rollup boundaries must hold. The latest touch keeps
    last_occurred_at paired with last_mcp_tool_call_id (the 0040 backfill and the
    timeline headline assume that pairing), and the earliest touch keeps
    first_occurred_at at the real first call (chat-title anchoring orders links
    by it). Linking an OLDER call after a NEWER one (across a DB reload) must not
    move the latest pointer onto the older call, but must pull first_occurred_at
    back to it.
    """
    service = McpChatService()
    later = datetime(2026, 5, 30, 0, 10, tzinfo=UTC)
    earlier = datetime(2026, 5, 30, 0, 5, tzinfo=UTC)
    with session_factory.begin() as session:
        workspace = session.scalar(select(Workspace).where(Workspace.slug == "default"))
        client = session.scalar(
            select(MCPClient).where(MCPClient.workspace_id == workspace.id)
        )
        page = ContextPage(
            workspace_id=workspace.id,
            slug="out-of-order-page",
            title="Out Of Order Page",
            page_type="overview",
        )
        session.add(page)
        session.flush()
        session_uuid = uuid4()
        later_call = _tool_call(
            client_id=client.id,
            workspace_id=workspace.id,
            mcp_session_id=session_uuid,
            tool_name="update_draft",
            request_id=uuid4(),
        )
        later_call.completed_at = later
        earlier_call = _tool_call(
            client_id=client.id,
            workspace_id=workspace.id,
            mcp_session_id=session_uuid,
            tool_name="update_draft",
            request_id=uuid4(),
        )
        earlier_call.completed_at = earlier
        session.add_all([later_call, earlier_call])
        session.flush()
        chat = service.upsert_session_from_tool_call(
            session=session,
            workspace_id=workspace.id,
            client_id=client.id,
            mcp_session_id=str(session_uuid),
            tool_call=later_call,
        )
        # Link the LATER (00:10) call first, then reload so last_occurred_at
        # round-trips (offset-naive on SQLite) like a real second request would.
        service.link_artifact(
            session=session,
            chat_session=chat,
            tool_call=later_call,
            resource_kind=McpChatArtifactKind.CONTEXT_PAGE,
            resource_id=page.id,
            verb=McpChatArtifactVerb.EDITED,
        )
        session.flush()
        session.expire_all()
        # Now link the EARLIER (00:05) call — out of timestamp order.
        service.link_artifact(
            session=session,
            chat_session=chat,
            tool_call=earlier_call,
            resource_kind=McpChatArtifactKind.CONTEXT_PAGE,
            resource_id=page.id,
            verb=McpChatArtifactVerb.EDITED,
        )
        session.flush()
        link = session.scalar(
            select(McpChatArtifactLink).where(
                McpChatArtifactLink.chat_session_id == chat.id,
                McpChatArtifactLink.resource_id == page.id,
            )
        )
        result_last_call = link.last_mcp_tool_call_id
        result_last_occurred = link.last_occurred_at
        result_first_occurred = link.first_occurred_at
        result_count = link.occurrence_count
        later_call_id = later_call.id

    # The rollup still points at the LATER (00:10) call, paired with its own
    # timestamp — not moved onto the older out-of-order call.
    assert result_last_call == later_call_id
    assert _as_utc(result_last_occurred) == later
    # Both rollup boundaries must reflect the real span: the out-of-order EARLIER
    # (00:05) call pulls first_occurred_at back even though it arrived second
    # (review follow-up 08bb502 — count and first_occurred_at must stay coherent).
    assert _as_utc(result_first_occurred) == earlier
    assert result_count == 2


def test_version_read_pointer_uses_its_own_clock_across_ordinary_references(
    session_factory,
):
    service = McpChatService()
    first_read_at = datetime(2026, 5, 30, 0, 5, tzinfo=UTC)
    ordinary_read_at = datetime(2026, 5, 30, 0, 10, tzinfo=UTC)
    second_read_at = datetime(2026, 5, 30, 0, 8, tzinfo=UTC)
    with session_factory.begin() as session:
        workspace = session.scalar(select(Workspace).where(Workspace.slug == "default"))
        client = session.scalar(
            select(MCPClient).where(MCPClient.workspace_id == workspace.id)
        )
        page = ContextPage(
            workspace_id=workspace.id,
            slug="version-read-clock-page",
            title="Version Read Clock Page",
            page_type="overview",
        )
        session.add(page)
        session.flush()
        first_version = ContextPageVersion(
            workspace_id=workspace.id,
            page_id=page.id,
            version_no=1,
            body_md="# Version one",
            content_hash="1" * 64,
            actor_type="human",
            created_by_actor_kind="human",
            created_by_actor_id="tester",
            version_status="draft",
        )
        second_version = ContextPageVersion(
            workspace_id=workspace.id,
            page_id=page.id,
            version_no=2,
            body_md="# Version two",
            content_hash="2" * 64,
            actor_type="human",
            created_by_actor_kind="human",
            created_by_actor_id="tester",
            version_status="draft",
        )
        session.add_all([first_version, second_version])
        session.flush()

        session_uuid = uuid4()
        calls = []
        for tool_name, occurred_at in (
            ("read_structure_map", first_read_at),
            ("read", ordinary_read_at),
            ("read_structure_map", second_read_at),
        ):
            call = _tool_call(
                client_id=client.id,
                workspace_id=workspace.id,
                mcp_session_id=session_uuid,
                tool_name=tool_name,
                request_id=uuid4(),
            )
            call.completed_at = occurred_at
            calls.append(call)
        session.add_all(calls)
        session.flush()
        chat = service.upsert_session_from_tool_call(
            session=session,
            workspace_id=workspace.id,
            client_id=client.id,
            mcp_session_id=str(session_uuid),
            tool_call=calls[0],
        )

        service.link_artifact(
            session=session,
            chat_session=chat,
            tool_call=calls[0],
            resource_kind=McpChatArtifactKind.CONTEXT_PAGE,
            resource_id=page.id,
            verb=McpChatArtifactVerb.REFERENCED,
            version_id=first_version.id,
            root_hash=first_version.content_hash,
        )
        service.link_artifact(
            session=session,
            chat_session=chat,
            tool_call=calls[1],
            resource_kind=McpChatArtifactKind.CONTEXT_PAGE,
            resource_id=page.id,
            verb=McpChatArtifactVerb.REFERENCED,
        )
        service.link_artifact(
            session=session,
            chat_session=chat,
            tool_call=calls[2],
            resource_kind=McpChatArtifactKind.CONTEXT_PAGE,
            resource_id=page.id,
            verb=McpChatArtifactVerb.REFERENCED,
            version_id=second_version.id,
            root_hash=second_version.content_hash,
        )
        session.flush()
        link = session.scalar(
            select(McpChatArtifactLink).where(
                McpChatArtifactLink.workspace_id == workspace.id,
                McpChatArtifactLink.chat_session_id == chat.id,
                McpChatArtifactLink.resource_id == page.id,
                McpChatArtifactLink.verb == McpChatArtifactVerb.REFERENCED.value,
            )
        )

        assert link.last_read_version_id == second_version.id
        assert link.last_read_root_hash == second_version.content_hash
        assert _as_utc(link.last_read_at) == second_read_at
        assert _as_utc(link.last_occurred_at) == ordinary_read_at


def test_mcp_chat_service_title_update_and_archive_emit_audit(session_factory):
    service = McpChatService()
    actor = ActorContext(actor_kind="human", actor_id=str(uuid4()))
    with session_factory.begin() as session:
        workspace = session.scalar(select(Workspace).where(Workspace.slug == "default"))
        client = session.scalar(
            select(MCPClient).where(MCPClient.workspace_id == workspace.id)
        )
        tool_call = _tool_call(
            client_id=client.id,
            workspace_id=workspace.id,
            mcp_session_id=uuid4(),
            tool_name="search",
            request_id=uuid4(),
        )
        session.add(tool_call)
        session.flush()
        chat = service.upsert_session_from_tool_call(
            session=session,
            workspace_id=workspace.id,
            client_id=client.id,
            mcp_session_id=str(tool_call.mcp_session_id),
            tool_call=tool_call,
        )

        updated = service.update_title(
            session=session,
            workspace_id=workspace.id,
            chat_session_id=chat.id,
            title="Reviewed MCP chat",
            actor=actor,
        )
        archived = service.archive(
            session=session,
            workspace_id=workspace.id,
            chat_session_id=chat.id,
            actor=actor,
        )

    assert updated.title == "Reviewed MCP chat"
    assert updated.title_user_edited is True
    assert archived.chat_session_id == chat.id
    assert archived.audit_event_id is not None


def test_cross_workspace_chat_is_opaque_and_foreign_artifact_link_is_hidden(
    session_factory,
):
    service = McpChatService()
    with session_factory.begin() as session:
        workspace = session.scalar(select(Workspace).where(Workspace.slug == "default"))
        client = session.scalar(
            select(MCPClient).where(MCPClient.workspace_id == workspace.id)
        )
        foreign_workspace = Workspace(
            slug="mcp-chat-foreign-scope",
            title="MCP Chat Foreign Scope",
            name="MCP Chat Foreign Scope",
        )
        session.add(foreign_workspace)
        session.flush()
        foreign_client = MCPClient(
            workspace_id=foreign_workspace.id,
            name="Foreign MCP client",
            client_type="generic",
            scopes=["read"],
        )
        foreign_page = ContextPage(
            workspace_id=foreign_workspace.id,
            slug="foreign-chat-linked-page",
            title="Foreign Chat Linked Page",
            page_type="overview",
        )
        session.add_all([foreign_client, foreign_page])
        session.flush()

        local_call = _tool_call(
            client_id=client.id,
            workspace_id=workspace.id,
            mcp_session_id=uuid4(),
            tool_name="read_page",
            request_id=uuid4(),
        )
        foreign_call = _tool_call(
            client_id=foreign_client.id,
            workspace_id=foreign_workspace.id,
            mcp_session_id=uuid4(),
            tool_name="read_page",
            request_id=uuid4(),
        )
        session.add_all([local_call, foreign_call])
        session.flush()
        local_chat = service.upsert_session_from_tool_call(
            session=session,
            workspace_id=workspace.id,
            client_id=client.id,
            mcp_session_id=str(local_call.mcp_session_id),
            tool_call=local_call,
        )
        foreign_chat = service.upsert_session_from_tool_call(
            session=session,
            workspace_id=foreign_workspace.id,
            client_id=foreign_client.id,
            mcp_session_id=str(foreign_call.mcp_session_id),
            tool_call=foreign_call,
        )
        service.link_artifact(
            session=session,
            chat_session=local_chat,
            tool_call=local_call,
            resource_kind=McpChatArtifactKind.CONTEXT_PAGE,
            resource_id=foreign_page.id,
            verb=McpChatArtifactVerb.REFERENCED,
        )
        actor_id = uuid4()
        actor = ActorContext(
            actor_kind="human",
            actor_id=str(actor_id),
            user_id=actor_id,
            token_kind="session",
            scopes=("read",),
            workspace_id=workspace.id,
            workspace_role="editor",
            role="editor",
        )
        detail = service.read_chat(
            session=session,
            workspace_id=workspace.id,
            chat_session_id=local_chat.id,
            actor=actor,
        )
        audit_ids_before = session.scalars(select(AuditEvent.id)).all()
        link_ids_before = session.scalars(select(McpChatArtifactLink.id)).all()
        foreign_title_before = foreign_chat.title

        with pytest.raises(ReadNotFoundError) as missing_error:
            service.archive(
                session=session,
                workspace_id=workspace.id,
                chat_session_id=uuid4(),
                actor=actor,
            )
        with pytest.raises(ReadNotFoundError) as foreign_error:
            service.archive(
                session=session,
                workspace_id=workspace.id,
                chat_session_id=foreign_chat.id,
                actor=actor,
            )

        audit_ids_after = session.scalars(select(AuditEvent.id)).all()
        link_ids_after = session.scalars(select(McpChatArtifactLink.id)).all()

    serialized = detail.model_dump_json()
    assert detail.linked_artifacts == []
    assert all(item.linked_artifacts == [] for item in detail.timeline)
    assert str(foreign_page.id) not in serialized
    assert foreign_page.title not in serialized
    assert str(foreign_error.value) == str(missing_error.value) == "MCP chat not found"
    assert str(foreign_workspace.id) not in str(foreign_error.value)
    assert foreign_chat.title == foreign_title_before
    assert foreign_chat.archived_at is None
    assert audit_ids_after == audit_ids_before
    assert link_ids_after == link_ids_before


def test_is_legacy_count_title_matches_machine_encodings_only():
    # Legacy machine forms heal on read; clean human/anchor titles must not.
    assert _is_legacy_count_title("1 actions")
    assert _is_legacy_count_title("3 tool calls")
    assert _is_legacy_count_title("3 tool calls, 1 write")
    assert _is_legacy_count_title("OAuth client dyn_abc · 1 actions · context_page")
    # Clean titles (artifact anchor, search anchor, fallback) stay untouched.
    assert not _is_legacy_count_title("Created: MCP Linked Page")
    assert not _is_legacy_count_title("Search: citation policy")
    assert not _is_legacy_count_title("MCP activity")
    assert not _is_legacy_count_title("Quarterly actions roadmap")


def test_read_chat_rederives_legacy_count_title_when_not_user_edited(session_factory):
    # M-B: a row written before the Sprint-6 title fix carries a machine count
    # string. read_chat must re-derive a clean anchor at read time (guarded by
    # title_user_edited) instead of serving the legacy string verbatim.
    service = McpChatService()
    with session_factory.begin() as session:
        workspace = session.scalar(select(Workspace).where(Workspace.slug == "default"))
        client = session.scalar(
            select(MCPClient).where(MCPClient.workspace_id == workspace.id)
        )
        page = ContextPage(
            workspace_id=workspace.id,
            slug="legacy-title-page",
            title="Legacy Title Page",
            page_type="overview",
        )
        session.add(page)
        session.flush()
        call = _tool_call(
            client_id=client.id,
            workspace_id=workspace.id,
            mcp_session_id=uuid4(),
            tool_name="write_draft",
            request_id=uuid4(),
        )
        session.add(call)
        session.flush()
        chat = service.upsert_session_from_tool_call(
            session=session,
            workspace_id=workspace.id,
            client_id=client.id,
            mcp_session_id=str(call.mcp_session_id),
            tool_call=call,
        )
        service.link_artifact(
            session=session,
            chat_session=chat,
            tool_call=call,
            resource_kind=McpChatArtifactKind.CONTEXT_PAGE,
            resource_id=page.id,
            verb=McpChatArtifactVerb.CREATED,
        )
        session.flush()
        # Simulate a legacy stored title that pre-dates the title fix.
        chat.title = "OAuth client dyn_abc · 1 actions · context_page"
        chat.title_user_edited = False
        session.flush()

        detail = service.read_chat(
            session=session,
            workspace_id=workspace.id,
            chat_session_id=chat.id,
        )

    # The legacy count string is re-derived to the clean artifact anchor.
    assert detail.title == "Legacy Title Page"
    assert "·" not in detail.title
    assert "actions" not in detail.title


def test_read_chat_keeps_user_edited_count_like_title(session_factory):
    # The re-derive is guarded by title_user_edited: a user who deliberately
    # named a chat with a count-like string keeps it verbatim.
    service = McpChatService()
    actor = ActorContext(actor_kind="human", actor_id=str(uuid4()))
    with session_factory.begin() as session:
        workspace = session.scalar(select(Workspace).where(Workspace.slug == "default"))
        client = session.scalar(
            select(MCPClient).where(MCPClient.workspace_id == workspace.id)
        )
        call = _tool_call(
            client_id=client.id,
            workspace_id=workspace.id,
            mcp_session_id=uuid4(),
            tool_name="search",
            request_id=uuid4(),
        )
        session.add(call)
        session.flush()
        chat = service.upsert_session_from_tool_call(
            session=session,
            workspace_id=workspace.id,
            client_id=client.id,
            mcp_session_id=str(call.mcp_session_id),
            tool_call=call,
        )
        service.update_title(
            session=session,
            workspace_id=workspace.id,
            chat_session_id=chat.id,
            title="3 tool calls",
            actor=actor,
        )
        detail = service.read_chat(
            session=session,
            workspace_id=workspace.id,
            chat_session_id=chat.id,
        )

    assert detail.title == "3 tool calls"
    assert detail.title_user_edited is True


def test_mcp_chat_service_unarchive_clears_archived_at_and_emits_audit(session_factory):
    service = McpChatService()
    actor = ActorContext(actor_kind="human", actor_id=str(uuid4()))
    with session_factory.begin() as session:
        workspace = session.scalar(select(Workspace).where(Workspace.slug == "default"))
        client = session.scalar(
            select(MCPClient).where(MCPClient.workspace_id == workspace.id)
        )
        call = _tool_call(
            client_id=client.id,
            workspace_id=workspace.id,
            mcp_session_id=uuid4(),
            tool_name="search",
            request_id=uuid4(),
        )
        session.add(call)
        session.flush()
        chat = service.upsert_session_from_tool_call(
            session=session,
            workspace_id=workspace.id,
            client_id=client.id,
            mcp_session_id=str(call.mcp_session_id),
            tool_call=call,
        )
        archived = service.archive(
            session=session,
            workspace_id=workspace.id,
            chat_session_id=chat.id,
            actor=actor,
        )
        assert archived.archived_at is not None
        assert chat.archived_at is not None

        unarchived = service.unarchive(
            session=session,
            workspace_id=workspace.id,
            chat_session_id=chat.id,
            actor=actor,
        )

    assert unarchived.chat_session_id == chat.id
    assert unarchived.archived_at is None
    assert unarchived.audit_event_id is not None
    assert chat.archived_at is None


def test_mcp_chat_service_lists_read_only_sessions_by_default(session_factory):
    service = McpChatService()
    with session_factory.begin() as session:
        workspace = session.scalar(select(Workspace).where(Workspace.slug == "default"))
        client = session.scalar(
            select(MCPClient).where(MCPClient.workspace_id == workspace.id)
        )
        read_call = _tool_call(
            client_id=client.id,
            workspace_id=workspace.id,
            mcp_session_id=uuid4(),
            tool_name="search",
            request_id=uuid4(),
        )
        write_call = _tool_call(
            client_id=client.id,
            workspace_id=workspace.id,
            mcp_session_id=uuid4(),
            tool_name="cite",
            request_id=uuid4(),
        )
        session.add_all([read_call, write_call])
        session.flush()
        service.upsert_session_from_tool_call(
            session=session,
            workspace_id=workspace.id,
            client_id=client.id,
            mcp_session_id=str(read_call.mcp_session_id),
            tool_call=read_call,
        )
        service.upsert_session_from_tool_call(
            session=session,
            workspace_id=workspace.id,
            client_id=client.id,
            mcp_session_id=str(write_call.mcp_session_id),
            tool_call=write_call,
        )

        all_chats = service.list_chats(session=session, workspace_id=workspace.id)
        writes_only = service.list_chats(
            session=session,
            workspace_id=workspace.id,
            writes_only=True,
        )

    assert {item.write_count for item in all_chats.items} == {0, 1}
    assert [item.write_count for item in writes_only.items] == [1]


def test_conversation_token_resolution_reuses_owned_and_rejects_unknown(
    session_factory,
):
    service = McpChatService()
    owner_id = uuid4()
    other_id = uuid4()
    with session_factory.begin() as session:
        workspace = session.scalar(select(Workspace).where(Workspace.slug == "default"))
        client = session.scalar(
            select(MCPClient).where(MCPClient.workspace_id == workspace.id)
        )
        second_client = MCPClient(
            workspace_id=workspace.id,
            name="second-token-client",
            client_type="generic",
            scopes=["*"],
            status="active",
        )
        other_workspace = Workspace(
            slug="other-token-workspace",
            title="Other token workspace",
            name="Other token workspace",
        )
        session.add(second_client)
        session.add(other_workspace)
        session.flush()
        tool_call = _tool_call(
            client_id=client.id,
            workspace_id=workspace.id,
            mcp_session_id=uuid4(),
            tool_name="search",
            request_id=uuid4(),
        )
        session.add(tool_call)
        session.flush()
        chat = service.upsert_session_from_tool_call(
            session=session,
            workspace_id=workspace.id,
            client_id=client.id,
            mcp_session_id="conv_owned",
            tool_call=tool_call,
            owner_user_id=owner_id,
        )
        client_chat = service.upsert_session_from_tool_call(
            session=session,
            workspace_id=workspace.id,
            client_id=client.id,
            mcp_session_id="conv_client",
            tool_call=tool_call,
        )
        workspace_id = workspace.id
        other_workspace_id = other_workspace.id
        client_id = client.id
        second_client_id = second_client.id

    owned = resolve_conversation_token(
        session_factory=session_factory,
        actor=ActorContext(
            actor_kind="human",
            actor_id=str(owner_id),
            user_id=owner_id,
        ),
        requested_token=chat.mcp_session_id,
        requested_workspace_authorized=True,
    )
    unknown = resolve_conversation_token(
        session_factory=session_factory,
        actor=ActorContext(
            actor_kind="human",
            actor_id=str(owner_id),
            user_id=owner_id,
        ),
        requested_token="conv_missing",
        requested_workspace_authorized=True,
    )
    cross_owner = resolve_conversation_token(
        session_factory=session_factory,
        actor=ActorContext(
            actor_kind="human",
            actor_id=str(other_id),
            user_id=other_id,
        ),
        requested_token=chat.mcp_session_id,
        requested_workspace_authorized=True,
    )
    cross_workspace = resolve_conversation_token(
        session_factory=session_factory,
        actor=ActorContext(
            actor_kind="human",
            actor_id=str(owner_id),
            user_id=owner_id,
        ),
        requested_token=chat.mcp_session_id,
        requested_workspace_id=other_workspace_id,
        requested_workspace_authorized=True,
    )
    retired_direct_actor = resolve_conversation_token(
        session_factory=session_factory,
        actor=ActorContext(
            actor_kind="mcp_client",
            actor_id=str(client_id),
            workspace_id=workspace_id,
            token_kind="mcp_client",
            token_id=client_id,
        ),
        requested_token=client_chat.mcp_session_id,
        requested_workspace_authorized=True,
    )
    cross_client = resolve_conversation_token(
        session_factory=session_factory,
        actor=ActorContext(
            actor_kind="mcp_client",
            actor_id=str(second_client_id),
            workspace_id=workspace_id,
            token_kind="mcp_client",
            token_id=second_client_id,
        ),
        requested_token=client_chat.mcp_session_id,
        requested_workspace_authorized=True,
    )

    assert owned.ok is True
    assert owned.token == "conv_owned"
    assert unknown.error_code == "conversation_token_unknown"
    assert unknown.token.startswith("conv_")
    # Non-owned cases are indistinguishable from unknown tokens — a single
    # error code keeps the resolution from acting as an existence oracle.
    assert cross_owner.error_code == "conversation_token_unknown"
    assert cross_owner.token.startswith("conv_")
    assert cross_workspace.error_code == "conversation_token_unknown"
    assert cross_workspace.token.startswith("conv_")
    # Direct MCPClient credentials belonged to the removed stdio transport.
    # Even a matching persisted client id cannot claim a conversation now;
    # authenticated PAT/OAuth actors use their human owner identity instead.
    assert retired_direct_actor.error_code == "conversation_token_unknown"
    assert retired_direct_actor.token.startswith("conv_")
    assert cross_client.error_code == "conversation_token_unknown"
    assert cross_client.token.startswith("conv_")


def test_trace_capture_normalizes_nested_resource_ids_to_surface_ids(
    session_factory,
):
    with session_factory.begin() as session:
        workspace = session.scalar(select(Workspace).where(Workspace.slug == "default"))
        page = ContextPage(
            workspace_id=workspace.id,
            slug="claim-linked-page",
            title="Claim Linked Page",
            page_type="overview",
        )
        source = LibraryItem(
            workspace_id=workspace.id,
            connector_type="file",
            format_hint="text",
            uri="file://source.txt",
            title="Source Asset",
        )
        session.add_all([page, source])
        session.flush()
        page_version = ContextPageVersion(
            workspace_id=page.workspace_id,
            page_id=page.id,
            version_no=1,
            body_md="# Claim",
            content_hash="claim-v1",
            actor_type="human",
            created_by_actor_kind="human",
            created_by_actor_id="user-1",
        )
        library_item_version = LibraryItemVersion(
            workspace_id=source.workspace_id,
            library_item_id=source.id,
            version_no=1,
            content_hash="source-v1",
            raw_storage_ref="raw/source.txt",
        )
        session.add_all([page_version, library_item_version])
        session.flush()
        claim = ContextClaim(
            workspace_id=page_version.workspace_id,
            page_version_id=page_version.id,
            claim_text="The source is linked.",
            location_in_page={},
        )
        artifact = LibraryArtifact(
            workspace_id=library_item_version.workspace_id,
            library_item_version_id=library_item_version.id,
            attempt_id=uuid4(),
            stage="conversion",
            blob_workspace_id=library_item_version.workspace_id,
            blob_id=uuid4(),
            blob_epoch=1,
            role="markdown",
            kind="text",
            media_type="text/plain",
            content_sha256="c" * 64,
            byte_size=1,
        )
        session.add_all([claim, artifact])
        session.flush()

        search_links = derive_artifact_links(
            session=session,
            tool_name="search",
            arguments={"query": "source"},
            payload={
                "data": {
                    "ranked_results": [
                        {"id": str(claim.id), "type": "context_claim"},
                        {"id": str(artifact.id), "type": "library_artifact"},
                    ]
                }
            },
        )
        citation_links = derive_artifact_links(
            session=session,
            tool_name="cite",
            arguments={},
            payload={
                "data": {
                    "citation_candidates": [{"library_artifact_id": str(artifact.id)}]
                }
            },
        )

    assert (
        McpChatArtifactKind.CONTEXT_PAGE,
        page.id,
        McpChatArtifactVerb.SEARCH_MATCH,
    ) in _link_keys(search_links)
    assert (
        McpChatArtifactKind.LIBRARY_ITEM,
        source.id,
        McpChatArtifactVerb.SEARCH_MATCH,
    ) in _link_keys(search_links)
    assert _link_keys(citation_links) == {
        (
            McpChatArtifactKind.LIBRARY_ITEM,
            source.id,
            McpChatArtifactVerb.CITED,
        )
    }


def test_trace_capture_uses_content_chunk_document_id_for_search_links(
    session_factory,
):
    with session_factory.begin() as session:
        workspace = session.scalar(select(Workspace).where(Workspace.slug == "default"))
        page = ContextPage(
            workspace_id=workspace.id,
            slug="semantic-linked-page",
            title="Semantic Linked Page",
            page_type="overview",
        )
        source = LibraryItem(
            workspace_id=workspace.id,
            connector_type="file",
            format_hint="text",
            uri="file://semantic-source.txt",
            title="Semantic Source",
        )
        session.add_all([page, source])
        session.flush()
        page_chunk_id = uuid4()
        source_chunk_id = uuid4()
        links = derive_artifact_links(
            session=session,
            tool_name="search",
            arguments={"query": "semantic"},
            payload={
                "data": {
                    "ranked_results": [
                        {
                            "id": str(page_chunk_id),
                            "type": "context_page",
                            "source_ref": {
                                "kind": "content_chunk",
                                "id": str(page_chunk_id),
                                "document_kind": "context_page",
                                "document_id": str(page.id),
                            },
                        },
                        {
                            "id": str(source_chunk_id),
                            "type": "library_item",
                            "source_ref": {
                                "kind": "content_chunk",
                                "id": str(source_chunk_id),
                                "document_kind": "library_item",
                                "document_id": str(source.id),
                            },
                        },
                    ]
                }
            },
        )

    assert (
        McpChatArtifactKind.CONTEXT_PAGE,
        page.id,
        McpChatArtifactVerb.SEARCH_MATCH,
    ) in _link_keys(links)
    assert (
        McpChatArtifactKind.LIBRARY_ITEM,
        source.id,
        McpChatArtifactVerb.SEARCH_MATCH,
    ) in _link_keys(links)
    assert all(
        link.resource_id not in {page_chunk_id, source_chunk_id} for link in links
    )


def _tool_call(
    *,
    client_id,
    workspace_id,
    mcp_session_id,
    tool_name: str,
    request_id,
    input_summary: dict | None = None,
) -> MCPToolCall:
    return MCPToolCall(
        client_id=client_id,
        workspace_id=workspace_id,
        mcp_session_id=mcp_session_id,
        request_id=request_id,
        tool_name=tool_name,
        input_summary=(
            {"query": "citation policy"} if input_summary is None else input_summary
        ),
        result_status="succeeded",
        result_summary="Tool call succeeded.",
        warnings=[],
        provider="unknown",
        model_id="unknown",
        params_summary={},
        client_self_reported=False,
    )


def test_derive_artifact_links_submit_revision_links_edited_page():
    # Codex #11: the revision must link its edited page to chat provenance.
    page_id = uuid4()
    links = derive_artifact_links(
        session=None,
        tool_name="submit_revision_for_review",
        arguments={"update": {"page_id": str(page_id)}},
        payload={"data": {}},
    )
    assert _link_keys(links) == {
        (McpChatArtifactKind.CONTEXT_PAGE, page_id, McpChatArtifactVerb.EDITED)
    }


def test_ensure_chat_session_creates_row_and_is_idempotent(session_factory):
    # Codex #10: the chat row exists on the first write so chat_owner resolves.
    from backend.models.mcp_chat import McpChatSession
    from backend.services.mcp_chat_service import ensure_chat_session

    with session_factory.begin() as session:
        workspace = session.scalar(select(Workspace).where(Workspace.slug == "default"))
        client = session.scalar(
            select(MCPClient).where(MCPClient.workspace_id == workspace.id)
        )
        first = ensure_chat_session(
            session,
            workspace_id=workspace.id,
            client_id=client.id,
            mcp_session_id="conv-token-10",
        )
        second = ensure_chat_session(
            session,
            workspace_id=workspace.id,
            client_id=client.id,
            mcp_session_id="conv-token-10",
        )
        assert first is not None
        assert first == second
        count = session.scalar(
            select(func.count())
            .select_from(McpChatSession)
            .where(
                McpChatSession.workspace_id == workspace.id,
                McpChatSession.mcp_session_id == "conv-token-10",
            )
        )
        assert count == 1


def test_ensure_chat_session_recovers_from_concurrent_insert(
    session_factory, monkeypatch
):
    # #11: a concurrent first-touch already inserted the (workspace, session)
    # row, so the INSERT hits uq_mcp_chat_session_per_workspace. ensure_chat_session
    # must re-read the existing row, not let the IntegrityError roll back the write.
    from backend.models.mcp_chat import McpChatSession
    from backend.services.mcp_chat_service import ensure_chat_session

    with session_factory.begin() as seed:
        workspace = seed.scalar(select(Workspace).where(Workspace.slug == "default"))
        client = seed.scalar(
            select(MCPClient).where(MCPClient.workspace_id == workspace.id)
        )
        ws_id, client_id = workspace.id, client.id
        winner = ensure_chat_session(
            seed, workspace_id=ws_id, client_id=client_id, mcp_session_id="race-token"
        )

    # Fresh session whose find-SELECT is forced to miss once, so the INSERT path
    # runs and collides with the committed winner row.
    session = session_factory()
    real_scalar = session.scalar
    state = {"n": 0}

    def scalar_miss_once(stmt, *args, **kwargs):
        state["n"] += 1
        if state["n"] == 1:
            return None  # simulate the not-yet-visible concurrent insert
        return real_scalar(stmt, *args, **kwargs)

    monkeypatch.setattr(session, "scalar", scalar_miss_once)
    try:
        got = ensure_chat_session(
            session,
            workspace_id=ws_id,
            client_id=client_id,
            mcp_session_id="race-token",
        )
        assert got == winner  # recovered the existing row instead of raising
    finally:
        session.rollback()
        session.close()

    with session_factory.begin() as check:
        count = check.scalar(
            select(func.count())
            .select_from(McpChatSession)
            .where(
                McpChatSession.workspace_id == ws_id,
                McpChatSession.mcp_session_id == "race-token",
            )
        )
        assert count == 1


def test_upsert_session_recovers_from_concurrent_insert(session_factory, monkeypatch):
    # #6: upsert_session_from_tool_call had the same unguarded SELECT-then-INSERT
    # as ensure_chat_session; a concurrent first trace must fold onto the winner
    # row instead of aborting the trace-capture transaction.
    service = McpChatService()
    with session_factory.begin() as seed:
        workspace = seed.scalar(select(Workspace).where(Workspace.slug == "default"))
        client = seed.scalar(
            select(MCPClient).where(MCPClient.workspace_id == workspace.id)
        )
        ws_id, client_id = workspace.id, client.id
        winner_call = _tool_call(
            client_id=client_id,
            workspace_id=ws_id,
            mcp_session_id=uuid4(),
            tool_name="cite",
            request_id=uuid4(),
        )
        seed.add(winner_call)
        seed.flush()
        winner_id = service.upsert_session_from_tool_call(
            session=seed,
            workspace_id=ws_id,
            client_id=client_id,
            mcp_session_id="race-upsert",
            tool_call=winner_call,
        ).id

    session = session_factory()
    loser_call = _tool_call(
        client_id=client_id,
        workspace_id=ws_id,
        mcp_session_id=uuid4(),
        tool_name="cite",
        request_id=uuid4(),
    )
    session.add(loser_call)
    session.flush()
    real_scalar = session.scalar
    state = {"n": 0}

    def scalar_miss_once(stmt, *args, **kwargs):
        state["n"] += 1
        if state["n"] == 1:
            return None  # simulate the not-yet-visible concurrent insert
        return real_scalar(stmt, *args, **kwargs)

    monkeypatch.setattr(session, "scalar", scalar_miss_once)
    try:
        chat = service.upsert_session_from_tool_call(
            session=session,
            workspace_id=ws_id,
            client_id=client_id,
            mcp_session_id="race-upsert",
            tool_call=loser_call,
        )
        assert chat.id == winner_id  # folded onto the surviving session
        # the loser trace's counters advance on the re-read (surviving) row, not
        # a discarded one (winner seeded 1 cite call -> now 2). (#11/#13)
        assert chat.tool_call_count == 2
        assert chat.write_count == 2
        assert chat.error_count == 0
    finally:
        session.rollback()
        session.close()


def test_resolve_trace_mcp_client_recovers_from_concurrent_insert(
    session_factory, monkeypatch
):
    # #12: resolve_trace_mcp_client had the same unguarded SELECT-then-INSERT;
    # a concurrent first-touch must fold onto the surviving client row.
    from backend.services.mcp_client_service import resolve_trace_mcp_client

    user_id = uuid4()
    grant_id = uuid4()

    def make_actor(scopes):
        # One authenticated OAuth grant keeps the same stable binding while
        # differing scopes make the post-recovery field-fold observable.
        return ActorContext(
            actor_kind="human",
            actor_id=str(user_id),
            scopes=scopes,
            user_id=user_id,
            token_kind="oauth_jwt",
            token_id=grant_id,
            client_id="concurrent-insert-client",
            provenance="oauth_client",
        )

    with session_factory.begin() as seed:
        ws_id = seed.scalar(select(Workspace).where(Workspace.slug == "default")).id
        # winner seeded with EMPTY scopes so the loser's fold is observable.
        winner_id = resolve_trace_mcp_client(
            seed, workspace_id=ws_id, actor=make_actor(())
        ).id

    session = session_factory()
    real_scalar = session.scalar
    state = {"n": 0}

    def scalar_miss_once(stmt, *args, **kwargs):
        state["n"] += 1
        if state["n"] == 1:
            return None  # name lookup misses -> forces the insert to collide
        return real_scalar(stmt, *args, **kwargs)

    monkeypatch.setattr(session, "scalar", scalar_miss_once)
    try:
        client = resolve_trace_mcp_client(
            session, workspace_id=ws_id, actor=make_actor(("mcp:read",))
        )
        assert client.id == winner_id  # converged on the surviving client
        # the fold re-ran on the re-read row: loser's scopes landed (not winner's []).
        assert list(client.scopes) == ["mcp:read"]
        assert client.status == "active"
    finally:
        session.rollback()
        session.close()


def test_resolve_trace_mcp_client_preserves_disabled_status_and_issued_fields(
    session_factory,
):
    # Trace capture resolves an identity; it must not undo an owner's Settings
    # disable or mirror the caller's current token scopes onto a row that was
    # already provisioned (scopes stay as issued, status stays as set).
    from backend.services.mcp_client_service import resolve_trace_mcp_client

    user_id = uuid4()
    grant_id = uuid4()

    def make_actor(scopes):
        return ActorContext(
            actor_kind="human",
            actor_id=str(user_id),
            scopes=scopes,
            user_id=user_id,
            token_kind="oauth_jwt",
            token_id=grant_id,
            client_id="preserve-disabled-client",
            provenance="oauth_client",
        )

    with session_factory.begin() as session:
        ws_id = session.scalar(select(Workspace).where(Workspace.slug == "default")).id
        client = resolve_trace_mcp_client(
            session, workspace_id=ws_id, actor=make_actor(("mcp:read",))
        )
        client_id = client.id
        # Owner-only Settings toggle (PATCH /settings/mcp-clients/{id}).
        client.status = "disabled"

    with session_factory.begin() as session:
        client = resolve_trace_mcp_client(
            session, workspace_id=ws_id, actor=make_actor(("mcp:write",))
        )
        assert client.id == client_id  # trace still lands on the same row
        assert client.status == "disabled"  # not silently re-activated
        assert list(client.scopes) == ["mcp:read"]  # as issued, not mirrored
        assert client.issued_to_user_id == user_id


def test_resolve_trace_mcp_client_oauth_rows_are_per_user(session_factory):
    # Two users of the SAME OAuth app must get distinct trace-identity rows;
    # a shared row would conflate issued_to_user_id and scopes across users.
    from backend.services.mcp_client_service import resolve_trace_mcp_client

    user_a, user_b = uuid4(), uuid4()
    grant_a, grant_b = uuid4(), uuid4()

    def oauth_actor(user_id, grant_id, scopes):
        return ActorContext(
            actor_kind="human",
            actor_id=str(user_id),
            scopes=scopes,
            user_id=user_id,
            token_kind="oauth_jwt",
            token_id=grant_id,
            client_id="oauth-app-identifier",
            provenance="oauth_client",
        )

    with session_factory.begin() as session:
        ws_id = session.scalar(select(Workspace).where(Workspace.slug == "default")).id
        row_a = resolve_trace_mcp_client(
            session,
            workspace_id=ws_id,
            actor=oauth_actor(user_a, grant_a, ("mcp:read",)),
        )
        row_b = resolve_trace_mcp_client(
            session,
            workspace_id=ws_id,
            actor=oauth_actor(user_b, grant_b, ("mcp:write",)),
        )
        assert row_a.id != row_b.id
        assert row_a.issued_to_user_id == user_a
        assert row_b.issued_to_user_id == user_b
        assert list(row_a.scopes) == ["mcp:read"]
        assert list(row_b.scopes) == ["mcp:write"]


def test_resolve_trace_mcp_client_oauth_grant_is_not_treated_as_api_token(
    session_factory,
):
    from backend.services.mcp_client_service import resolve_trace_mcp_client

    user_id = uuid4()

    def oauth_actor(grant_id):
        return ActorContext(
            actor_kind="human",
            actor_id=str(user_id),
            scopes=("mcp:read",),
            user_id=user_id,
            token_kind="oauth_jwt",
            token_id=grant_id,
            client_id="oauth-app-identifier",
        )

    grant_id = uuid4()
    with session_factory.begin() as session:
        ws_id = session.scalar(select(Workspace).where(Workspace.slug == "default")).id
        first = resolve_trace_mcp_client(
            session, workspace_id=ws_id, actor=oauth_actor(grant_id)
        )
        first_id = first.id
        first_binding_key = first.runtime_binding_key
        assert first.client_type == "oauth"
        assert first.name.startswith("OAuth client oauth-app-identifier")

    with session_factory.begin() as session:
        refreshed = resolve_trace_mcp_client(
            session, workspace_id=ws_id, actor=oauth_actor(grant_id)
        )
        assert refreshed.id == first_id
        assert refreshed.client_type == "oauth"

        separately_authorized = resolve_trace_mcp_client(
            session, workspace_id=ws_id, actor=oauth_actor(uuid4())
        )
        assert separately_authorized.id != first_id
        assert separately_authorized.runtime_binding_key != first_binding_key


def test_distinct_pat_bindings_with_same_legacy_name_prefix_do_not_alias(
    session_factory,
):
    """A display-name collision cannot transfer a disabled PAT lifecycle."""

    from backend.services.mcp_client_service import resolve_trace_mcp_client

    first_token_id = UUID("12345678-0000-4000-8000-000000000001")
    second_token_id = UUID("12345678-0000-4000-8000-000000000002")
    user_id = uuid4()

    with session_factory.begin() as session:
        workspace = session.scalar(select(Workspace).where(Workspace.slug == "default"))
        session.add_all(
            [
                ApiToken(
                    id=token_id,
                    user_id=user_id,
                    name="Shared laptop",
                    token_lookup_hash=f"lookup-{token_id}",
                    hashed_token=f"hash-{token_id}",
                    scopes=["mcp:read"],
                    mcp_client_type="generic",
                    mcp_client_label="Shared laptop",
                )
                for token_id in (first_token_id, second_token_id)
            ]
        )
        session.add(
            MCPClient(
                workspace_id=workspace.id,
                name="Shared laptop (12345678)",
                client_type="generic",
                scopes=["mcp:read"],
                status="disabled",
                issued_to_user_id=user_id,
                runtime_binding_key=f"api_token:{first_token_id}",
                token_metadata={"token_id": str(first_token_id)},
            )
        )
        workspace_id = workspace.id

    second_actor = ActorContext(
        actor_kind="human",
        actor_id=str(user_id),
        scopes=("mcp:read",),
        user_id=user_id,
        token_kind="api_token",
        token_id=second_token_id,
    )
    with session_factory.begin() as session:
        resolved = resolve_trace_mcp_client(
            session,
            workspace_id=workspace_id,
            actor=second_actor,
            enforce_active=True,
        )
        assert resolved.runtime_binding_key == f"api_token:{second_token_id}"
        assert resolved.status == "active"
        assert str(second_token_id) in resolved.name
        assert (
            session.scalar(
                select(MCPClient).where(
                    MCPClient.runtime_binding_key == f"api_token:{first_token_id}"
                )
            ).status
            == "disabled"
        )


def test_distinct_oauth_grants_with_same_legacy_prefix_do_not_alias_tombstone(
    session_factory,
):
    """A newly authorized grant receives a fresh binding despite name overlap."""

    from backend.services.mcp_client_service import resolve_trace_mcp_client

    first_grant_id = UUID("abcdef12-0000-4000-8000-000000000001")
    second_grant_id = UUID("abcdef12-0000-4000-8000-000000000002")
    user_id = uuid4()
    client_id = "oauth-client-with-a-shared-display-prefix"

    def actor(grant_id: UUID) -> ActorContext:
        return ActorContext(
            actor_kind="human",
            actor_id=str(user_id),
            scopes=("mcp:read",),
            user_id=user_id,
            token_kind="oauth_jwt",
            token_id=grant_id,
            client_id=client_id,
        )

    with session_factory.begin() as session:
        workspace_id = session.scalar(
            select(Workspace.id).where(Workspace.slug == "default")
        )
        session.add(
            MCPClient(
                workspace_id=workspace_id,
                name=(
                    f"OAuth client {client_id[:24]} "
                    f"(user {str(user_id)[:8]}) (grant abcdef12)"
                ),
                client_type="oauth",
                scopes=["mcp:read"],
                status="disabled",
                issued_to_user_id=user_id,
                runtime_binding_key=f"oauth_grant:{first_grant_id}",
                token_metadata={"token_id": str(first_grant_id)},
            )
        )

    with session_factory.begin() as session:
        resolved = resolve_trace_mcp_client(
            session,
            workspace_id=workspace_id,
            actor=actor(second_grant_id),
            enforce_active=True,
        )
        assert resolved.runtime_binding_key == f"oauth_grant:{second_grant_id}"
        assert resolved.status == "active"
        assert str(second_grant_id) in resolved.name
        assert client_id in resolved.name


def test_uuid_shaped_oauth_registration_cannot_claim_direct_client_identity(
    session_factory,
):
    from backend.services.mcp_client_service import resolve_trace_mcp_client

    user_id = uuid4()
    grant_id = uuid4()
    with session_factory.begin() as session:
        workspace_id = session.scalar(
            select(Workspace.id).where(Workspace.slug == "default")
        )
        direct = MCPClient(
            workspace_id=workspace_id,
            name="direct-client",
            client_type="generic",
            scopes=["mcp:read"],
            status="disabled",
            token_metadata={},
        )
        session.add(direct)
        session.flush()
        direct_id = direct.id

    oauth_actor = ActorContext(
        actor_kind="human",
        actor_id=str(user_id),
        scopes=("mcp:read",),
        user_id=user_id,
        token_kind="oauth_jwt",
        token_id=grant_id,
        # A DCR client_id is not an MCPClient primary key, even when it parses
        # as a UUID supplied by a registration profile.
        client_id=str(direct_id),
    )
    with session_factory.begin() as session:
        resolved = resolve_trace_mcp_client(
            session,
            workspace_id=workspace_id,
            actor=oauth_actor,
        )

    assert resolved.id != direct_id
    assert resolved.runtime_binding_key == f"oauth_grant:{grant_id}"


def test_trace_pat_identity_reuses_legacy_row_and_avoids_prefix_collision(
    session_factory,
):
    from backend.services.mcp_client_service import resolve_trace_mcp_client

    token_a = UUID("aaaaaaaa-0000-4000-8000-000000000001")
    token_b = UUID("aaaaaaaa-0000-4000-8000-000000000002")

    with session_factory.begin() as session:
        workspace = session.scalar(select(Workspace).where(Workspace.slug == "default"))
        user = User(
            email="trace-pat-collision@example.com",
            password_hash="test-hash",
            display_name="Trace PAT Collision",
        )
        session.add(user)
        session.flush()
        session.add_all(
            [
                ApiToken(
                    id=token_id,
                    user_id=user.id,
                    name=f"Trace PAT {index}",
                    token_lookup_hash=f"trace-pat-lookup-{index}",
                    hashed_token=f"trace-pat-hash-{index}",
                    scopes=["mcp:read"],
                )
                for index, token_id in enumerate((token_a, token_b), start=1)
            ]
        )
        legacy = MCPClient(
            workspace_id=workspace.id,
            name="API token (aaaaaaaa)",
            client_type="api_token",
            scopes=["mcp:read"],
            status="disabled",
            issued_to_user_id=user.id,
            token_metadata={
                "token_kind": "api_token",
                "token_id": str(token_a),
                "oauth_client_id": None,
            },
        )
        session.add(legacy)
        session.flush()
        legacy_id = legacy.id

        def pat_actor(token_id):
            return ActorContext(
                actor_kind="human",
                actor_id=str(user.id),
                scopes=("mcp:read",),
                user_id=user.id,
                token_kind="api_token",
                token_id=token_id,
                provenance="personal_access_token",
            )

        row_a = resolve_trace_mcp_client(
            session,
            workspace_id=workspace.id,
            actor=pat_actor(token_a),
        )
        row_b = resolve_trace_mcp_client(
            session,
            workspace_id=workspace.id,
            actor=pat_actor(token_b),
        )

        assert row_a.id == legacy_id
        assert row_a.status == "disabled"
        assert row_b.id != row_a.id
        assert row_b.token_metadata["token_id"] == str(token_b)
        assert row_b.name.endswith(f"({token_b})")


def test_resolve_trace_mcp_client_does_not_treat_oauth_uuid_as_internal_client_id(
    session_factory,
):
    from backend.services.mcp_client_service import resolve_trace_mcp_client

    with session_factory.begin() as session:
        workspace = session.scalar(select(Workspace).where(Workspace.slug == "default"))
        user = User(
            email="trace-oauth-uuid@example.com",
            password_hash="test-hash",
            display_name="Trace OAuth UUID",
        )
        session.add(user)
        session.flush()
        internal = MCPClient(
            workspace_id=workspace.id,
            name="native-mcp-client",
            client_type="generic",
            scopes=["mcp:read"],
            status="active",
        )
        session.add(internal)
        session.flush()

        actor = ActorContext(
            actor_kind="human",
            actor_id=str(user.id),
            scopes=("mcp:read",),
            user_id=user.id,
            token_kind="oauth_jwt",
            token_id=uuid4(),
            client_id=str(internal.id),
            provenance="oauth_client",
        )
        resolved = resolve_trace_mcp_client(
            session,
            workspace_id=workspace.id,
            actor=actor,
        )

        assert resolved.id != internal.id
        assert resolved.client_type == "oauth"
        assert resolved.token_metadata["oauth_client_id"] == str(internal.id)


def test_upsert_session_last_activity_never_regresses_on_naive_round_trip(
    session_factory,
):
    """_later must keep the newer stored timestamp even when SQLite hands it
    back offset-naive while the incoming (older, out-of-order) trace timestamp
    is aware — pre-fix the TypeError fallback returned the incoming value and
    last_activity_at moved backwards, diverging from Postgres."""
    service = McpChatService()
    later = datetime(2026, 5, 30, 0, 10, tzinfo=UTC)
    earlier = datetime(2026, 5, 30, 0, 5, tzinfo=UTC)
    with session_factory.begin() as session:
        workspace = session.scalar(select(Workspace).where(Workspace.slug == "default"))
        client = session.scalar(
            select(MCPClient).where(MCPClient.workspace_id == workspace.id)
        )
        session_uuid = uuid4()
        newer_call = _tool_call(
            client_id=client.id,
            workspace_id=workspace.id,
            mcp_session_id=session_uuid,
            tool_name="search",
            request_id=uuid4(),
        )
        newer_call.completed_at = later
        session.add(newer_call)
        session.flush()
        chat = service.upsert_session_from_tool_call(
            session=session,
            workspace_id=workspace.id,
            client_id=client.id,
            mcp_session_id=str(session_uuid),
            tool_call=newer_call,
        )
        session.flush()
        # Reload so last_activity_at round-trips (offset-naive on SQLite).
        session.expire(chat)
        older_call = _tool_call(
            client_id=client.id,
            workspace_id=workspace.id,
            mcp_session_id=session_uuid,
            tool_name="search",
            request_id=uuid4(),
        )
        older_call.completed_at = earlier
        session.add(older_call)
        session.flush()
        chat = service.upsert_session_from_tool_call(
            session=session,
            workspace_id=workspace.id,
            client_id=client.id,
            mcp_session_id=str(session_uuid),
            tool_call=older_call,
        )
        assert _as_utc(chat.last_activity_at) == later


def test_chat_keeps_first_client_and_timeline_spans_clients(session_factory):
    """Continuing one conversation token through a second client identity must
    not re-point chat.client_id, and the timeline must still include the calls
    made through BOTH clients, matching tool_call_count."""
    service = McpChatService()
    with session_factory.begin() as session:
        workspace = session.scalar(select(Workspace).where(Workspace.slug == "default"))
        first_client = session.scalar(
            select(MCPClient).where(MCPClient.workspace_id == workspace.id)
        )
        second_client = MCPClient(
            workspace_id=workspace.id,
            name="second-chat-client",
            client_type="generic",
            scopes=["*"],
            status="active",
        )
        session.add(second_client)
        session.flush()
        session_uuid = uuid4()
        first_call = _tool_call(
            client_id=first_client.id,
            workspace_id=workspace.id,
            mcp_session_id=session_uuid,
            tool_name="search",
            request_id=uuid4(),
        )
        second_call = _tool_call(
            client_id=second_client.id,
            workspace_id=workspace.id,
            mcp_session_id=session_uuid,
            tool_name="search",
            request_id=uuid4(),
        )
        session.add_all([first_call, second_call])
        session.flush()
        chat = service.upsert_session_from_tool_call(
            session=session,
            workspace_id=workspace.id,
            client_id=first_client.id,
            mcp_session_id=str(session_uuid),
            tool_call=first_call,
        )
        chat = service.upsert_session_from_tool_call(
            session=session,
            workspace_id=workspace.id,
            client_id=second_client.id,
            mcp_session_id=str(session_uuid),
            tool_call=second_call,
        )
        assert chat.client_id == first_client.id

        detail = service.read_chat(
            session=session,
            workspace_id=workspace.id,
            chat_session_id=chat.id,
        )
        assert detail.tool_call_count == 2
        assert {row.id for row in detail.timeline} == {first_call.id, second_call.id}


def test_link_artifact_rollup_recovers_from_concurrent_insert(
    session_factory, monkeypatch
):
    # #10: the rollup-link insert has a SAVEPOINT + IntegrityError re-read; a
    # concurrent first-link must fold the occurrence onto the surviving row
    # (occurrence_count incremented once, not doubled).
    from backend.models.mcp_chat import McpChatSession

    service = McpChatService()
    with session_factory.begin() as seed:
        workspace = seed.scalar(select(Workspace).where(Workspace.slug == "default"))
        client = seed.scalar(
            select(MCPClient).where(MCPClient.workspace_id == workspace.id)
        )
        ws_id, client_id = workspace.id, client.id
        page = ContextPage(
            workspace_id=ws_id,
            slug="race-link-page",
            title="Race Link Page",
            page_type="overview",
        )
        seed.add(page)
        seed.flush()
        page_id = page.id
        call = _tool_call(
            client_id=client_id,
            workspace_id=ws_id,
            mcp_session_id=uuid4(),
            tool_name="write_draft",
            request_id=uuid4(),
        )
        seed.add(call)
        seed.flush()
        chat = service.upsert_session_from_tool_call(
            session=seed,
            workspace_id=ws_id,
            client_id=client_id,
            mcp_session_id="race-link",
            tool_call=call,
        )
        chat_id = chat.id
        winner_link_id = service.link_artifact(
            session=seed,
            chat_session=chat,
            tool_call=call,
            resource_kind=McpChatArtifactKind.CONTEXT_PAGE,
            resource_id=page_id,
            verb=McpChatArtifactVerb.CREATED,
        ).id

    session = session_factory()
    chat = session.get(McpChatSession, chat_id)
    loser_call = _tool_call(
        client_id=client_id,
        workspace_id=ws_id,
        mcp_session_id=uuid4(),
        tool_name="write_draft",
        request_id=uuid4(),
    )
    session.add(loser_call)
    session.flush()
    real_scalar = session.scalar
    state = {"n": 0}

    def scalar_miss_once(stmt, *args, **kwargs):
        state["n"] += 1
        if state["n"] == 1:
            return None  # rollup-link lookup misses -> forces the insert collide
        return real_scalar(stmt, *args, **kwargs)

    monkeypatch.setattr(session, "scalar", scalar_miss_once)
    try:
        link = service.link_artifact(
            session=session,
            chat_session=chat,
            tool_call=loser_call,
            resource_kind=McpChatArtifactKind.CONTEXT_PAGE,
            resource_id=page_id,
            verb=McpChatArtifactVerb.CREATED,
        )
        assert link.id == winner_link_id
        assert link.occurrence_count == 2  # folded once via _advance, not doubled
    finally:
        session.rollback()
        session.close()


def test_upsert_session_counters_increment_atomically(session_factory):
    # Counter race: increments must be SQL-side (count = count + 1) so a
    # concurrent trace's committed increments (simulated by a core UPDATE the
    # identity-mapped row never sees) are not overwritten by stale
    # Python-computed values under Postgres READ COMMITTED.
    from backend.models.mcp_chat import McpChatSession

    service = McpChatService()
    with session_factory.begin() as session:
        workspace = session.scalar(select(Workspace).where(Workspace.slug == "default"))
        client = session.scalar(
            select(MCPClient).where(MCPClient.workspace_id == workspace.id)
        )
        first_call = _tool_call(
            client_id=client.id,
            workspace_id=workspace.id,
            mcp_session_id=uuid4(),
            tool_name="cite",
            request_id=uuid4(),
        )
        first_call.result_status = "failed"
        session.add(first_call)
        session.flush()
        chat = service.upsert_session_from_tool_call(
            session=session,
            workspace_id=workspace.id,
            client_id=client.id,
            mcp_session_id="atomic-counters",
            tool_call=first_call,
        )
        # Bump the row behind the identity map, like a concurrent committed
        # trace would.
        session.execute(
            update(McpChatSession)
            .where(McpChatSession.id == chat.id)
            .values(tool_call_count=5, write_count=3, error_count=2)
        )
        second_call = _tool_call(
            client_id=client.id,
            workspace_id=workspace.id,
            mcp_session_id=uuid4(),
            tool_name="cite",
            request_id=uuid4(),
        )
        second_call.result_status = "failed"
        session.add(second_call)
        session.flush()
        chat = service.upsert_session_from_tool_call(
            session=session,
            workspace_id=workspace.id,
            client_id=client.id,
            mcp_session_id="atomic-counters",
            tool_call=second_call,
        )
        # +1 lands on the DB values (5/3/2), not on the stale in-memory 1/1/1.
        assert chat.tool_call_count == 6
        assert chat.write_count == 4
        assert chat.error_count == 3


def test_link_artifact_occurrence_count_increments_atomically(session_factory):
    # Same counter race as the session counters: occurrence_count must advance
    # via SQL expression so a concurrent link's committed +1 is not lost.
    service = McpChatService()
    with session_factory.begin() as session:
        workspace = session.scalar(select(Workspace).where(Workspace.slug == "default"))
        client = session.scalar(
            select(MCPClient).where(MCPClient.workspace_id == workspace.id)
        )
        page = ContextPage(
            workspace_id=workspace.id,
            slug="atomic-occurrence-page",
            title="Atomic Occurrence Page",
            page_type="overview",
        )
        session.add(page)
        session.flush()
        session_uuid = uuid4()
        first_call = _tool_call(
            client_id=client.id,
            workspace_id=workspace.id,
            mcp_session_id=session_uuid,
            tool_name="write_draft",
            request_id=uuid4(),
        )
        second_call = _tool_call(
            client_id=client.id,
            workspace_id=workspace.id,
            mcp_session_id=session_uuid,
            tool_name="write_draft",
            request_id=uuid4(),
        )
        session.add_all([first_call, second_call])
        session.flush()
        chat = service.upsert_session_from_tool_call(
            session=session,
            workspace_id=workspace.id,
            client_id=client.id,
            mcp_session_id=str(session_uuid),
            tool_call=first_call,
        )
        link = service.link_artifact(
            session=session,
            chat_session=chat,
            tool_call=first_call,
            resource_kind=McpChatArtifactKind.CONTEXT_PAGE,
            resource_id=page.id,
            verb=McpChatArtifactVerb.CREATED,
        )
        # Bump the rollup behind the identity map, like a concurrent committed
        # link would.
        session.execute(
            update(McpChatArtifactLink)
            .where(McpChatArtifactLink.id == link.id)
            .values(occurrence_count=7)
        )
        link = service.link_artifact(
            session=session,
            chat_session=chat,
            tool_call=second_call,
            resource_kind=McpChatArtifactKind.CONTEXT_PAGE,
            resource_id=page.id,
            verb=McpChatArtifactVerb.CREATED,
        )
        # +1 lands on the DB value (7), not on the stale in-memory 1.
        assert link.occurrence_count == 8


def test_link_artifact_edge_swallows_concurrent_insert(session_factory, monkeypatch):
    # #10: the per-call edge insert swallows a concurrent duplicate
    # (IntegrityError -> pass) and leaves exactly one edge row.
    from backend.models.mcp_chat import McpChatSession

    service = McpChatService()
    with session_factory.begin() as seed:
        workspace = seed.scalar(select(Workspace).where(Workspace.slug == "default"))
        client = seed.scalar(
            select(MCPClient).where(MCPClient.workspace_id == workspace.id)
        )
        ws_id, client_id = workspace.id, client.id
        page = ContextPage(
            workspace_id=ws_id,
            slug="race-edge-page",
            title="Race Edge Page",
            page_type="overview",
        )
        seed.add(page)
        seed.flush()
        page_id = page.id
        call = _tool_call(
            client_id=client_id,
            workspace_id=ws_id,
            mcp_session_id=uuid4(),
            tool_name="write_draft",
            request_id=uuid4(),
        )
        seed.add(call)
        seed.flush()
        call_id = call.id
        chat = service.upsert_session_from_tool_call(
            session=seed,
            workspace_id=ws_id,
            client_id=client_id,
            mcp_session_id="race-edge",
            tool_call=call,
        )
        chat_id = chat.id
        # seed the rollup link AND the per-call edge for this exact call.
        service.link_artifact(
            session=seed,
            chat_session=chat,
            tool_call=call,
            resource_kind=McpChatArtifactKind.CONTEXT_PAGE,
            resource_id=page_id,
            verb=McpChatArtifactVerb.CREATED,
        )

    session = session_factory()
    chat = session.get(McpChatSession, chat_id)
    call = session.get(MCPToolCall, call_id)
    real_scalar = session.scalar
    state = {"n": 0}

    def scalar_miss_on_edge(stmt, *args, **kwargs):
        state["n"] += 1
        # 1st scalar = rollup-link lookup (real, finds the winner); 2nd scalar =
        # the per-call edge lookup -> miss, forcing the edge insert to collide.
        if state["n"] == 2:
            return None
        return real_scalar(stmt, *args, **kwargs)

    monkeypatch.setattr(session, "scalar", scalar_miss_on_edge)
    try:
        service.link_artifact(
            session=session,
            chat_session=chat,
            tool_call=call,
            resource_kind=McpChatArtifactKind.CONTEXT_PAGE,
            resource_id=page_id,
            verb=McpChatArtifactVerb.CREATED,
        )
        edge_count = real_scalar(
            select(func.count())
            .select_from(McpChatToolCallArtifact)
            .where(McpChatToolCallArtifact.mcp_tool_call_id == call_id)
        )
        assert edge_count == 1  # duplicate insert was swallowed, not duplicated
    finally:
        session.rollback()
        session.close()


def _link_keys(links):
    return {(link.resource_kind, link.resource_id, link.verb) for link in links}


HIDDEN_TITLE = "Merger Codename Bluebird"


def test_the_list_card_does_not_name_a_page_the_reader_cannot_see(session_factory):
    """The timeline blanks a call that touched a hidden target. The list card's
    title and summary fall back to the SAME call's raw arguments and did not,
    so the page's title and the agent's stated reason surfaced there instead.

    The existing redaction test could not catch this: its fixture's
    `input_summary` was `{"query": "citation policy"}`, which never collides
    with the hidden page's title no matter what leaks.
    """

    service = McpChatService()
    with session_factory.begin() as session:
        workspace = session.scalar(select(Workspace).where(Workspace.slug == "default"))
        client = session.scalar(
            select(MCPClient).where(MCPClient.workspace_id == workspace.id)
        )
        page = ContextPage(
            workspace_id=workspace.id,
            slug="hidden-anchor",
            title=HIDDEN_TITLE,
            page_type="overview",
            lifecycle_status="archived",
        )
        session.add(page)
        session.flush()
        page_id = page.id
        call = _tool_call(
            client_id=client.id,
            workspace_id=workspace.id,
            mcp_session_id=uuid4(),
            tool_name="write_draft",
            request_id=uuid4(),
            # Exactly what the client sent, captured verbatim.
            input_summary={
                "title": HIDDEN_TITLE,
                "reason": f"record the {HIDDEN_TITLE} decision",
            },
        )
        session.add(call)
        session.flush()
        chat = service.upsert_session_from_tool_call(
            session=session,
            workspace_id=workspace.id,
            client_id=client.id,
            mcp_session_id=str(call.mcp_session_id),
            tool_call=call,
        )
        service.link_artifact(
            session=session,
            chat_session=chat,
            tool_call=call,
            resource_kind=McpChatArtifactKind.CONTEXT_PAGE,
            resource_id=page_id,
            verb=McpChatArtifactVerb.CREATED,
        )
        session.flush()

        viewer = ActorContext(
            actor_kind="human",
            actor_id="viewer",
            token_kind="session",
            scopes=("read",),
            workspace_id=workspace.id,
            workspace_role="viewer",
            role="viewer",
        )
        listed = service.list_chats(
            session=session,
            workspace_id=workspace.id,
            actor=viewer,
        )
        detail = service.read_chat(
            session=session,
            workspace_id=workspace.id,
            chat_session_id=chat.id,
            actor=viewer,
        )

    assert listed.items, "the chat itself is still listed; only its labels are gated"
    card = listed.items[0]
    assert HIDDEN_TITLE not in card.title
    assert HIDDEN_TITLE not in card.summary
    assert card.subject is None
    assert HIDDEN_TITLE not in detail.title
    assert HIDDEN_TITLE not in (detail.summary or "")
    assert detail.subject is None


def test_the_summary_lookup_does_not_load_every_call_in_the_chat(
    session_factory, monkeypatch
):
    """`input_summary` holds the full arguments, `body_markdown` included, so
    materializing them all to look at the first was megabytes per chat -- and
    the list did it twice for every chat on the page."""

    service = McpChatService()
    loaded_all = []
    original = McpChatService._tool_calls_for_chat

    def _spy(self, session, chat):
        rows = original(self, session, chat)
        loaded_all.append(len(rows))
        return rows

    monkeypatch.setattr(McpChatService, "_tool_calls_for_chat", _spy)

    with session_factory.begin() as session:
        workspace = session.scalar(select(Workspace).where(Workspace.slug == "default"))
        client = session.scalar(
            select(MCPClient).where(MCPClient.workspace_id == workspace.id)
        )
        mcp_session_id = uuid4()
        for index in range(6):
            call = _tool_call(
                client_id=client.id,
                workspace_id=workspace.id,
                mcp_session_id=mcp_session_id,
                tool_name="write_draft",
                request_id=uuid4(),
                input_summary={"title": f"Call {index}", "body_markdown": "x" * 20_000},
            )
            session.add(call)
            session.flush()
            service.upsert_session_from_tool_call(
                session=session,
                workspace_id=workspace.id,
                client_id=client.id,
                mcp_session_id=str(mcp_session_id),
                tool_call=call,
            )
        session.flush()

        viewer = ActorContext(
            actor_kind="human",
            actor_id="viewer",
            token_kind="session",
            scopes=("read",),
            workspace_id=workspace.id,
            workspace_role="viewer",
            role="viewer",
        )
        loaded_all.clear()
        listed = service.list_chats(
            session=session,
            workspace_id=workspace.id,
            actor=viewer,
        )

    assert listed.items
    # The list card derives its title and summary from the chat's first call.
    # Reaching that through the full materializer is the defect; the bounded
    # single-row lookup is the fix.
    assert loaded_all == [], f"list_chats materialized {loaded_all} tool calls"


SECRET_REASON = "record why the Bluebird terms changed"


def test_the_card_reports_what_the_client_said_it_was_doing(session_factory):
    """MCP delivers a tool name and arguments; the user's own prompt never
    reaches the server, so intent can only come from the natural-language field
    the client had to fill in.
    `submit_context_edit_for_review.reason` is required and was read by
    nothing."""

    service = McpChatService()
    with session_factory.begin() as session:
        workspace = session.scalar(select(Workspace).where(Workspace.slug == "default"))
        client = session.scalar(
            select(MCPClient).where(MCPClient.workspace_id == workspace.id)
        )
        call = _tool_call(
            client_id=client.id,
            workspace_id=workspace.id,
            mcp_session_id=uuid4(),
            tool_name="submit_context_edit_for_review",
            request_id=uuid4(),
            input_summary={
                "title": "Retention Policy",
                "reason": "capture the retention decision from the Q3 review",
            },
        )
        session.add(call)
        session.flush()
        service.upsert_session_from_tool_call(
            session=session,
            workspace_id=workspace.id,
            client_id=client.id,
            mcp_session_id=str(call.mcp_session_id),
            tool_call=call,
        )
        session.flush()
        listed = service.list_chats(session=session, workspace_id=workspace.id)

    card = listed.items[0]
    assert card.intent_summary == "capture the retention decision from the Q3 review"
    # The source travels with the text so the screen can attribute it to the
    # agent instead of letting a reader take it for something they said.
    assert card.intent_source == "write_reason"


def test_a_search_only_chat_reports_the_query_as_its_intent(session_factory):
    service = McpChatService()
    with session_factory.begin() as session:
        workspace = session.scalar(select(Workspace).where(Workspace.slug == "default"))
        client = session.scalar(
            select(MCPClient).where(MCPClient.workspace_id == workspace.id)
        )
        page = ContextPage(
            workspace_id=workspace.id,
            slug="search-only-result",
            title="Search Only Result",
            page_type="overview",
        )
        session.add(page)
        call = _tool_call(
            client_id=client.id,
            workspace_id=workspace.id,
            mcp_session_id=uuid4(),
            tool_name="search",
            request_id=uuid4(),
            input_summary={"query": "how long do we keep audit logs"},
        )
        session.add(call)
        session.flush()
        chat = service.upsert_session_from_tool_call(
            session=session,
            workspace_id=workspace.id,
            client_id=client.id,
            mcp_session_id=str(call.mcp_session_id),
            tool_call=call,
        )
        service.link_artifact(
            session=session,
            chat_session=chat,
            tool_call=call,
            resource_kind=McpChatArtifactKind.CONTEXT_PAGE,
            resource_id=page.id,
            verb=McpChatArtifactVerb.SEARCH_MATCH,
        )
        session.flush()
        listed = service.list_chats(session=session, workspace_id=workspace.id)
        detail = service.read_chat(
            session=session,
            workspace_id=workspace.id,
            chat_session_id=chat.id,
        )

    card = listed.items[0]
    assert card.intent_summary == "how long do we keep audit logs"
    assert card.intent_source == "search_query"
    assert card.summary == "how long do we keep audit logs"
    assert not card.summary.startswith("Search match:")
    assert card.linked_artifacts == []
    assert [item.verb for item in detail.linked_artifacts] == ["search_match"]
    assert [item.key for item in detail.timeline[0].input_display_items] == [
        "search_query"
    ]
    assert [item.key for item in detail.timeline[0].result_display_items] == [
        "search_match_artifact"
    ]


def test_artifact_provenance_keeps_reads_before_search_matches(session_factory):
    service = McpChatService()
    with session_factory.begin() as session:
        workspace = session.scalar(select(Workspace).where(Workspace.slug == "default"))
        client = session.scalar(
            select(MCPClient).where(MCPClient.workspace_id == workspace.id)
        )
        page = ContextPage(
            workspace_id=workspace.id,
            slug="search-and-read-result",
            title="Search And Read Result",
            page_type="overview",
        )
        session.add(page)
        session.flush()

        for tool_name, verb in (
            ("search", McpChatArtifactVerb.SEARCH_MATCH),
            ("read", McpChatArtifactVerb.REFERENCED),
        ):
            call = _tool_call(
                client_id=client.id,
                workspace_id=workspace.id,
                mcp_session_id=uuid4(),
                tool_name=tool_name,
                request_id=uuid4(),
            )
            session.add(call)
            session.flush()
            chat = service.upsert_session_from_tool_call(
                session=session,
                workspace_id=workspace.id,
                client_id=client.id,
                mcp_session_id=str(call.mcp_session_id),
                tool_call=call,
            )
            service.link_artifact(
                session=session,
                chat_session=chat,
                tool_call=call,
                resource_kind=McpChatArtifactKind.CONTEXT_PAGE,
                resource_id=page.id,
                verb=verb,
            )
        session.flush()

        provenance = service.list_artifact_provenance(
            session=session,
            workspace_id=workspace.id,
            resource_kind=McpChatArtifactKind.CONTEXT_PAGE,
            resource_id=page.id,
        )

    assert [item.verb for item in provenance.items] == [
        "referenced",
        "search_match",
    ]


def test_a_hidden_first_call_contributes_no_intent(session_factory):
    """The reason is client-authored text about a page this reader cannot see,
    so it is exactly as disclosive as the title the redaction already hides."""

    service = McpChatService()
    with session_factory.begin() as session:
        workspace = session.scalar(select(Workspace).where(Workspace.slug == "default"))
        client = session.scalar(
            select(MCPClient).where(MCPClient.workspace_id == workspace.id)
        )
        page = ContextPage(
            workspace_id=workspace.id,
            slug="hidden-intent",
            title="Hidden Intent Page",
            page_type="overview",
            lifecycle_status="archived",
        )
        session.add(page)
        session.flush()
        page_id = page.id
        call = _tool_call(
            client_id=client.id,
            workspace_id=workspace.id,
            mcp_session_id=uuid4(),
            tool_name="submit_context_edit_for_review",
            request_id=uuid4(),
            input_summary={"title": "Hidden Intent Page", "reason": SECRET_REASON},
        )
        session.add(call)
        session.flush()
        chat = service.upsert_session_from_tool_call(
            session=session,
            workspace_id=workspace.id,
            client_id=client.id,
            mcp_session_id=str(call.mcp_session_id),
            tool_call=call,
        )
        service.link_artifact(
            session=session,
            chat_session=chat,
            tool_call=call,
            resource_kind=McpChatArtifactKind.CONTEXT_PAGE,
            resource_id=page_id,
            verb=McpChatArtifactVerb.CREATED,
        )
        session.flush()
        viewer = ActorContext(
            actor_kind="human",
            actor_id="viewer",
            token_kind="session",
            scopes=("read",),
            workspace_id=workspace.id,
            workspace_role="viewer",
            role="viewer",
        )
        gated = service.list_chats(
            session=session, workspace_id=workspace.id, actor=viewer
        )
        ungated = service.list_chats(session=session, workspace_id=workspace.id)

    assert gated.items[0].intent_summary is None
    assert SECRET_REASON not in (gated.items[0].summary or "")
    assert ungated.items[0].intent_summary == SECRET_REASON


def test_a_refused_call_and_a_broken_one_are_counted_apart(session_factory):
    """`error_count` sums failed, rejected_by_policy and conflict, so an auditor
    cannot tell a tool that broke from a call workspace policy refused."""

    service = McpChatService()
    with session_factory.begin() as session:
        workspace = session.scalar(select(Workspace).where(Workspace.slug == "default"))
        client = session.scalar(
            select(MCPClient).where(MCPClient.workspace_id == workspace.id)
        )
        mcp_session_id = uuid4()
        for status in ("rejected_by_policy", "failed", "conflict"):
            call = _tool_call(
                client_id=client.id,
                workspace_id=workspace.id,
                mcp_session_id=mcp_session_id,
                tool_name="write_draft",
                request_id=uuid4(),
            )
            call.result_status = status
            session.add(call)
            session.flush()
            service.upsert_session_from_tool_call(
                session=session,
                workspace_id=workspace.id,
                client_id=client.id,
                mcp_session_id=str(mcp_session_id),
                tool_call=call,
            )
        session.flush()
        listed = service.list_chats(session=session, workspace_id=workspace.id)

    card = listed.items[0]
    assert card.error_count == 3
    assert card.rejected_count == 1
    # `conflict` is a broken write, not a policy refusal.
    assert card.failed_count == 2


def test_actor_bound_detail_and_export_redact_unlinked_error_calls(
    session_factory,
):
    """Error traces have no artifact edge, so their input ids are re-resolved.

    A failed, conflicted or refused call is gated on the targets its input
    names, under the reader's own visibility. For the editor every target is
    hidden -- an archived Context page, a Library id with no declared kind
    (untrusted input never proves what a bare UUID names), and an owner-only
    Agent page -- so each row keeps only its shape. The workspace owner may
    open that Agent page, so that one row is readable to them, message
    included because their client made the call; the other two stay
    shape-only for the owner as well. Chat ownership never widens target
    visibility. Only a trusted internal read retains the raw audit evidence.
    """

    service = McpChatService()
    with session_factory.begin() as session:
        workspace = session.scalar(select(Workspace).where(Workspace.slug == "default"))
        client = session.scalar(
            select(MCPClient).where(MCPClient.workspace_id == workspace.id)
        )
        owner = User(
            email="failed-chat-owner@example.com",
            password_hash="test-hash",
            display_name="Failed Chat Owner",
        )
        archived_page = ContextPage(
            workspace_id=workspace.id,
            slug="failed-archived-target",
            title="Archived Failure Target",
            page_type="decision",
            lifecycle_status="archived",
        )
        restricted_library = LibraryItem(
            workspace_id=workspace.id,
            connector_type="upload",
            format_hint="pdf",
            uri="https://example.com/restricted-failure.pdf",
            title="Restricted Failure Source",
            trust_level="restricted",
            lifecycle_status="registered",
        )
        owner_only_agent = AgentPage(
            workspace_id=workspace.id,
            category="memory",
            title="Owner-only Failure Memory",
            slug="owner-only-failure-memory",
            lifecycle_status="active",
            sensitivity="private",
        )
        session.add_all([owner, archived_page, restricted_library, owner_only_agent])
        session.flush()

        session_id = uuid4()
        specs = (
            (
                "conflict",
                "update_draft",
                "page_id",
                archived_page.id,
                "CONTEXT_ERROR_SECRET",
            ),
            (
                "failed",
                "read",
                "target_id",
                restricted_library.id,
                "LIBRARY_ERROR_SECRET",
            ),
            (
                "rejected_by_policy",
                "write_agent_page_draft",
                "agent_page_id",
                owner_only_agent.id,
                "AGENT_ERROR_SECRET",
            ),
        )
        chat = None
        for index, (status, tool_name, id_field, target_id, marker) in enumerate(specs):
            call = _tool_call(
                client_id=client.id,
                workspace_id=workspace.id,
                mcp_session_id=session_id,
                tool_name=tool_name,
                request_id=uuid4(),
                input_summary={
                    id_field: str(target_id),
                    "body_markdown": f"body {marker}",
                    "patch_description": f"patch {marker}",
                    "reason": f"reason {marker}",
                },
            )
            call.result_status = status
            call.result_summary = f"result {marker}"
            # The timeline is ordered by start time, and the assertions below
            # read it position by position against ``specs``. Left to the
            # column default each call would take the host clock at its INSERT,
            # and a step back between two of them reorders the rows.
            call.started_at = datetime(2026, 1, 1, 0, 0, index, tzinfo=UTC)
            session.add(call)
            session.flush()
            chat = service.upsert_session_from_tool_call(
                session=session,
                workspace_id=workspace.id,
                client_id=client.id,
                mcp_session_id=str(session_id),
                tool_call=call,
                owner_user_id=owner.id,
            )
        assert chat is not None
        session.flush()

        reader_id = uuid4()
        non_owner = ActorContext(
            actor_kind="human",
            actor_id=str(reader_id),
            user_id=reader_id,
            token_kind="session",
            scopes=("read",),
            workspace_id=workspace.id,
            workspace_role="editor",
            role="editor",
        )
        owner_actor = ActorContext(
            actor_kind="human",
            actor_id=str(owner.id),
            user_id=owner.id,
            token_kind="session",
            scopes=("read",),
            workspace_id=workspace.id,
            workspace_role="owner",
            role="owner",
        )
        gated = service.read_chat(
            session=session,
            workspace_id=workspace.id,
            chat_session_id=chat.id,
            actor=non_owner,
        )
        gated_export = service.export_chats(
            session=session,
            workspace_id=workspace.id,
            actor=non_owner,
        )
        owned = service.read_chat(
            session=session,
            workspace_id=workspace.id,
            chat_session_id=chat.id,
            actor=owner_actor,
        )
        owned_export = service.export_chats(
            session=session,
            workspace_id=workspace.id,
            actor=owner_actor,
        )
        ungated = service.read_chat(
            session=session,
            workspace_id=workspace.id,
            chat_session_id=chat.id,
        )

    gated_export_chat = next(item for item in gated_export.chats if item.id == gated.id)
    owned_export_chat = next(item for item in owned_export.chats if item.id == owned.id)
    markers = {spec[-1] for spec in specs}

    def assert_shape_only(entry, spec) -> None:
        status, _tool, id_field, _target, _marker = spec
        assert entry.result_status == status
        assert entry.redaction_reason == "target_hidden"
        # The call's shape survives; nothing that names or quotes it does.
        assert entry.input_summary == {
            "fields": sorted([id_field, "body_markdown", "patch_description", "reason"])
        }
        assert entry.input_availability == "redacted"
        assert entry.result_summary is None
        assert entry.result_availability == "redacted"
        assert entry.subject is None
        assert entry.headline == entry.tool_name.replace("_", " ")
        assert entry.linked_artifacts == []

    for projected in (gated, gated_export_chat):
        assert projected.title_source == "generic"
        assert projected.subject is None
        for entry, spec in zip(projected.timeline, specs, strict=True):
            assert_shape_only(entry, spec)
        serialized = projected.model_dump_json()
        assert all(marker not in serialized for marker in markers)
        assert all(str(spec[3]) not in serialized for spec in specs)

    for projected in (owned, owned_export_chat):
        assert projected.title_source == "generic"
        context_entry, library_entry, agent_entry = projected.timeline
        assert_shape_only(context_entry, specs[0])
        assert_shape_only(library_entry, specs[1])
        assert agent_entry.redaction_reason is None
        assert agent_entry.result_summary == "result AGENT_ERROR_SECRET"
        assert [
            (item.key, item.text, item.resource_id)
            for item in agent_entry.input_display_items
            if item.kind == "artifact"
        ] == [("target_artifact", "Owner-only Failure Memory", owner_only_agent.id)]
        serialized = projected.model_dump_json()
        assert "CONTEXT_ERROR_SECRET" not in serialized
        assert "LIBRARY_ERROR_SECRET" not in serialized
        assert str(archived_page.id) not in serialized
        assert str(restricted_library.id) not in serialized

    assert {
        entry.input_summary["body_markdown"].removeprefix("body ")
        for entry in ungated.timeline
    } == markers
    assert {
        entry.result_summary.removeprefix("result ")
        for entry in ungated.timeline
        if entry.result_summary is not None
    } == markers


def test_chat_search_does_not_answer_for_a_label_the_reader_cannot_see(
    session_factory,
):
    """`title` and `summary` are written at capture time with no actor, and the
    `q` predicate runs in SQL against those columns — so searching a hidden
    page's exact title returned that chat while any other query returned
    nothing. That difference is a working existence oracle, and re-deriving the
    label afterwards does not close it because the row is already in the result.
    """

    service = McpChatService()
    with session_factory.begin() as session:
        workspace = session.scalar(select(Workspace).where(Workspace.slug == "default"))
        client = session.scalar(
            select(MCPClient).where(MCPClient.workspace_id == workspace.id)
        )
        hidden_chats = []
        for index in range(2):
            page = ContextPage(
                workspace_id=workspace.id,
                slug=f"oracle-page-{index}",
                title=HIDDEN_TITLE,
                page_type="overview",
                lifecycle_status="archived",
            )
            session.add(page)
            session.flush()
            call = _tool_call(
                client_id=client.id,
                workspace_id=workspace.id,
                mcp_session_id=uuid4(),
                tool_name="write_draft",
                request_id=uuid4(),
                input_summary={"title": HIDDEN_TITLE, "reason": SECRET_REASON},
            )
            session.add(call)
            session.flush()
            chat = service.upsert_session_from_tool_call(
                session=session,
                workspace_id=workspace.id,
                client_id=client.id,
                mcp_session_id=str(call.mcp_session_id),
                tool_call=call,
            )
            service.link_artifact(
                session=session,
                chat_session=chat,
                tool_call=call,
                resource_kind=McpChatArtifactKind.CONTEXT_PAGE,
                resource_id=page.id,
                verb=McpChatArtifactVerb.CREATED,
            )
            hidden_chats.append(chat)
        session.flush()

        viewer = ActorContext(
            actor_kind="human",
            actor_id="viewer",
            token_kind="session",
            scopes=("read",),
            workspace_id=workspace.id,
            workspace_role="viewer",
            role="viewer",
        )
        # The stored label really does contain the secret, so the SQL predicate
        # really would match -- this is not a vacuous assertion.
        assert all(
            HIDDEN_TITLE in (chat.title or "") or HIDDEN_TITLE in (chat.summary or "")
            for chat in hidden_chats
        )
        hit = service.list_chats(
            session=session,
            workspace_id=workspace.id,
            actor=viewer,
            q=HIDDEN_TITLE,
            limit=1,
        )
        miss = service.list_chats(
            session=session,
            workspace_id=workspace.id,
            actor=viewer,
            q="a phrase that appears nowhere at all",
            limit=1,
        )
        hit_final = service.list_chats(
            session=session,
            workspace_id=workspace.id,
            actor=viewer,
            q=HIDDEN_TITLE,
            limit=1,
            cursor=hit.next_cursor,
        )
        miss_final = service.list_chats(
            session=session,
            workspace_id=workspace.id,
            actor=viewer,
            q="a phrase that appears nowhere at all",
            limit=1,
            cursor=miss.next_cursor,
        )
        ungated = service.list_chats(
            session=session, workspace_id=workspace.id, q=HIDDEN_TITLE
        )

    # A gated reader learns nothing from the difference between the two queries.
    assert hit.items == []
    assert miss.items == []
    assert hit.next_cursor == miss.next_cursor == "chat-search-v1:1"
    assert hit_final.items == miss_final.items == []
    assert hit_final.next_cursor is None
    assert miss_final.next_cursor is None
    # An ungated (system) read still finds it, so the search itself still works.
    assert len(ungated.items) == 2


def test_chat_search_paginates_after_actor_visible_projection(session_factory):
    service = McpChatService()
    needle = "public architecture pagination"
    base = datetime(2026, 8, 5, 5, 0, tzinfo=UTC)
    with session_factory.begin() as session:
        workspace = session.scalar(select(Workspace).where(Workspace.slug == "default"))
        client = session.scalar(
            select(MCPClient).where(MCPClient.workspace_id == workspace.id)
        )
        visible_chat, _visible_call = _record_chat_call(
            service=service,
            session=session,
            workspace=workspace,
            client=client,
            mcp_session_id=uuid4(),
            tool_name="search",
            input_summary={"query": needle},
            started_at=base,
        )
        for index in range(2):
            hidden_page = ContextPage(
                workspace_id=workspace.id,
                slug=f"pagination-hidden-{index}",
                title=f"{needle} hidden {index}",
                page_type="decision",
                lifecycle_status="archived",
            )
            session.add(hidden_page)
            session.flush()
            hidden_chat, hidden_call = _record_chat_call(
                service=service,
                session=session,
                workspace=workspace,
                client=client,
                mcp_session_id=uuid4(),
                tool_name="write_draft",
                input_summary={"title": hidden_page.title},
                started_at=base.replace(minute=index + 1),
            )
            service.link_artifact(
                session=session,
                chat_session=hidden_chat,
                tool_call=hidden_call,
                resource_kind=McpChatArtifactKind.CONTEXT_PAGE,
                resource_id=hidden_page.id,
                verb=McpChatArtifactVerb.CREATED,
            )

        reader_id = uuid4()
        reader = ActorContext(
            actor_kind="human",
            actor_id=str(reader_id),
            user_id=reader_id,
            token_kind="session",
            scopes=("read",),
            workspace_id=workspace.id,
            workspace_role="editor",
            role="editor",
        )
        visible_items = []
        cursor = None
        cursors = []
        for _ in range(3):
            visible_page = service.list_chats(
                session=session,
                workspace_id=workspace.id,
                actor=reader,
                q=needle,
                limit=1,
                cursor=cursor,
            )
            visible_items.extend(visible_page.items)
            cursor = visible_page.next_cursor
            cursors.append(cursor)
            if cursor is None:
                break
        raw_page = service.list_chats(
            session=session,
            workspace_id=workspace.id,
            q=needle,
            limit=1,
        )

    assert [item.id for item in visible_items] == [visible_chat.id]
    assert cursors == ["chat-search-v1:1", "chat-search-v1:2", None]
    assert raw_page.items[0].id != visible_chat.id
    assert raw_page.next_cursor == "1"


@pytest.fixture
def benchmark_lane(request):
    """Run wall-clock budgets serially through the explicit benchmark lane."""
    if os.environ.get("DTXWIKI_TEST_BENCHMARKS") != "1":
        pytest.skip("wall-clock budget; run `make test-backend-benchmarks`")
    if hasattr(request.config, "workerinput"):
        pytest.fail(
            "DTXWIKI_TEST_BENCHMARKS=1 under pytest-xdist: run the benchmark "
            "lane without -n, as `make test-backend-benchmarks` does",
            pytrace=False,
        )


def test_actor_search_query_and_latency_benchmark_for_1000_chats(
    session_factory,
    benchmark_lane,
):
    service = McpChatService()
    base = datetime(2026, 8, 5, 7, 0, tzinfo=UTC)
    with session_factory.begin() as session:
        workspace = session.scalar(select(Workspace).where(Workspace.slug == "default"))
        client = session.scalar(
            select(MCPClient).where(MCPClient.workspace_id == workspace.id)
        )
        session.add_all(
            [
                McpChatSession(
                    workspace_id=workspace.id,
                    client_id=client.id,
                    mcp_session_id=f"bounded-search-{index}",
                    started_at=base,
                    last_activity_at=base,
                    tool_call_count=0,
                    write_count=0,
                    error_count=0,
                    failed_count=0,
                    rejected_count=0,
                    title=f"Public activity {index}",
                    summary="No matching phrase",
                    title_user_edited=False,
                )
                for index in range(1000)
            ]
        )
        session.flush()
        session.expire_all()
        reader_id = uuid4()
        reader = ActorContext(
            actor_kind="human",
            actor_id=str(reader_id),
            user_id=reader_id,
            token_kind="session",
            scopes=("read",),
            workspace_id=workspace.id,
            workspace_role="editor",
            role="editor",
        )
        statement_count = 0

        def count_statement(*_args):
            nonlocal statement_count
            statement_count += 1

        engine = session.get_bind()
        event.listen(engine, "before_cursor_execute", count_statement)
        started = time.perf_counter()
        try:
            page = service.list_chats(
                session=session,
                workspace_id=workspace.id,
                actor=reader,
                q="needle absent from every chat",
                limit=50,
            )
        finally:
            elapsed = time.perf_counter() - started
            event.remove(engine, "before_cursor_execute", count_statement)

    assert page.items == []
    assert page.next_cursor == "chat-search-v1:20"
    assert statement_count <= 260
    assert elapsed < 1.5


def test_actor_artifact_links_query_is_constant_for_1000_hidden_rows(
    session_factory,
):
    service = McpChatService()
    base = datetime(2026, 8, 5, 8, 0, tzinfo=UTC)
    with session_factory.begin() as session:
        workspace = session.scalar(select(Workspace).where(Workspace.slug == "default"))
        client = session.scalar(
            select(MCPClient).where(MCPClient.workspace_id == workspace.id)
        )
        call = _tool_call(
            client_id=client.id,
            workspace_id=workspace.id,
            mcp_session_id=uuid4(),
            tool_name="read",
            request_id=uuid4(),
        )
        session.add(call)
        session.flush()
        chat = service.upsert_session_from_tool_call(
            session=session,
            workspace_id=workspace.id,
            client_id=client.id,
            mcp_session_id=str(call.mcp_session_id),
            tool_call=call,
        )
        hidden_items = [
            LibraryItem(
                workspace_id=workspace.id,
                connector_type="upload",
                format_hint="pdf",
                uri=f"https://example.com/bounded-hidden-{index}.pdf",
                title=f"Bounded Hidden Source {index}",
                trust_level="restricted",
                lifecycle_status="registered",
            )
            for index in range(1000)
        ]
        session.add_all(hidden_items)
        session.flush()
        session.add_all(
            [
                McpChatArtifactLink(
                    workspace_id=chat.workspace_id,
                    chat_session_id=chat.id,
                    resource_kind=McpChatArtifactKind.LIBRARY_ITEM.value,
                    resource_id=item.id,
                    verb=McpChatArtifactVerb.REFERENCED.value,
                    first_occurred_at=base,
                    last_occurred_at=base,
                    occurrence_count=1,
                    last_mcp_tool_call_id=call.id,
                )
                for item in hidden_items
            ]
        )
        session.flush()
        session.expire_all()
        reader_id = uuid4()
        reader = ActorContext(
            actor_kind="human",
            actor_id=str(reader_id),
            user_id=reader_id,
            token_kind="session",
            scopes=("read",),
            workspace_id=workspace.id,
            workspace_role="editor",
            role="editor",
        )
        statement_count = 0

        def count_statement(*_args):
            nonlocal statement_count
            statement_count += 1

        engine = session.get_bind()
        event.listen(engine, "before_cursor_execute", count_statement)
        started = time.perf_counter()
        try:
            page = service.list_artifact_links(
                session=session,
                workspace_id=workspace.id,
                actor=reader,
                limit=100,
            )
        finally:
            elapsed = time.perf_counter() - started
            event.remove(engine, "before_cursor_execute", count_statement)

    assert page.items == []
    assert page.next_cursor is None
    assert statement_count <= 2
    assert elapsed < 1.0


def _record_chat_call(
    *,
    service,
    session,
    workspace,
    client,
    mcp_session_id,
    tool_name,
    input_summary,
    status="succeeded",
    started_at=None,
    result_summary="Tool call succeeded.",
):
    call = _tool_call(
        client_id=client.id,
        workspace_id=workspace.id,
        mcp_session_id=mcp_session_id,
        tool_name=tool_name,
        request_id=uuid4(),
        input_summary=input_summary,
    )
    call.result_status = status
    call.result_summary = result_summary
    if started_at is not None:
        call.started_at = started_at
        call.completed_at = started_at
    session.add(call)
    session.flush()
    chat = service.upsert_session_from_tool_call(
        session=session,
        workspace_id=workspace.id,
        client_id=client.id,
        mcp_session_id=str(mcp_session_id),
        tool_call=call,
    )
    return chat, call


def test_chat_presentation_hierarchy_prefers_authoring_then_user_override(
    session_factory,
):
    service = McpChatService()
    base = datetime(2026, 8, 5, 1, 0, tzinfo=UTC)
    with session_factory.begin() as session:
        workspace = session.scalar(select(Workspace).where(Workspace.slug == "default"))
        client = session.scalar(
            select(MCPClient).where(MCPClient.workspace_id == workspace.id)
        )
        session_id = uuid4()
        chat, _guide = _record_chat_call(
            service=service,
            session=session,
            workspace=workspace,
            client=client,
            mcp_session_id=session_id,
            tool_name="guide",
            input_summary={"detail_level": "brief"},
            started_at=base,
        )
        _chat, _search = _record_chat_call(
            service=service,
            session=session,
            workspace=workspace,
            client=client,
            mcp_session_id=session_id,
            tool_name="search",
            input_summary={"query": "OAuth 연동"},
            started_at=base.replace(minute=1),
        )

        searched = service.read_chat(
            session=session,
            workspace_id=workspace.id,
            chat_session_id=chat.id,
        )
        assert (
            searched.title_source,
            searched.action,
            searched.subject,
            searched.primary_tool_name,
        ) == ("query", "search", "OAuth 연동", "search")

        page = ContextPage(
            workspace_id=workspace.id,
            slug="hierarchy-page",
            title="MCP 설정",
            page_type="overview",
        )
        session.add(page)
        session.flush()
        _chat, create_call = _record_chat_call(
            service=service,
            session=session,
            workspace=workspace,
            client=client,
            mcp_session_id=session_id,
            tool_name="write_draft",
            input_summary={"title": "MCP 설정", "reason": "document setup"},
            started_at=base.replace(minute=2),
        )
        service.link_artifact(
            session=session,
            chat_session=chat,
            tool_call=create_call,
            resource_kind=McpChatArtifactKind.CONTEXT_PAGE,
            resource_id=page.id,
            verb=McpChatArtifactVerb.CREATED,
        )

        authored = service.read_chat(
            session=session,
            workspace_id=workspace.id,
            chat_session_id=chat.id,
        )
        assert (
            authored.title_source,
            authored.action,
            authored.subject,
            authored.subject_kind,
        ) == ("artifact", "create", "MCP 설정", "context_page")

        actor = ActorContext(actor_kind="human", actor_id=str(uuid4()))
        service.update_title(
            session=session,
            workspace_id=workspace.id,
            chat_session_id=chat.id,
            title="내가 정한 제목",
            actor=actor,
        )
        _chat, edit_call = _record_chat_call(
            service=service,
            session=session,
            workspace=workspace,
            client=client,
            mcp_session_id=session_id,
            tool_name="update_draft",
            input_summary={"reason": "later edit"},
            started_at=base.replace(minute=3),
        )
        service.link_artifact(
            session=session,
            chat_session=chat,
            tool_call=edit_call,
            resource_kind=McpChatArtifactKind.CONTEXT_PAGE,
            resource_id=page.id,
            verb=McpChatArtifactVerb.EDITED,
        )
        renamed = service.read_chat(
            session=session,
            workspace_id=workspace.id,
            chat_session_id=chat.id,
        )

    assert renamed.title == "내가 정한 제목"
    assert renamed.title_source == "user"
    assert renamed.subject == "내가 정한 제목"


def test_chat_presentation_status_allowlist_is_positive_and_exhaustive(
    session_factory,
):
    service = McpChatService()
    eligible = {"accepted", "succeeded", "partial", "needs_review"}
    all_statuses = [
        "accepted",
        "succeeded",
        "partial",
        "needs_review",
        "failed",
        "rejected_by_policy",
        "conflict",
        "future_unknown",
    ]
    with session_factory.begin() as session:
        workspace = session.scalar(select(Workspace).where(Workspace.slug == "default"))
        client = session.scalar(
            select(MCPClient).where(MCPClient.workspace_id == workspace.id)
        )
        presentations = {}
        for status in all_statuses:
            chat, _call = _record_chat_call(
                service=service,
                session=session,
                workspace=workspace,
                client=client,
                mcp_session_id=uuid4(),
                tool_name="search",
                input_summary={"query": f"query {status}"},
                status=status,
                result_summary=("Forged user prompt: secret; 99 matching documents"),
            )
            presentations[status] = service.read_chat(
                session=session,
                workspace_id=workspace.id,
                chat_session_id=chat.id,
            )

    for status, presentation in presentations.items():
        if status in eligible:
            assert presentation.title_source == "query"
            assert presentation.subject == f"query {status}"
        else:
            assert presentation.title_source == "generic"
            assert presentation.subject is None
            assert presentation.title == "MCP activity"
        assert "Forged user prompt" not in presentation.title


def test_cited_then_create_edit_search_is_order_invariant_and_prefers_latest_authoring(
    session_factory,
):
    service = McpChatService()
    cited_at = datetime(2026, 8, 5, 1, 55, tzinfo=UTC)
    older_at = datetime(2026, 8, 5, 2, 0, tzinfo=UTC)
    latest_at = datetime(2026, 8, 5, 2, 5, tzinfo=UTC)
    search_at = datetime(2026, 8, 5, 2, 10, tzinfo=UTC)
    with session_factory.begin() as session:
        workspace = session.scalar(select(Workspace).where(Workspace.slug == "default"))
        client = session.scalar(
            select(MCPClient).where(MCPClient.workspace_id == workspace.id)
        )
        older_page = ContextPage(
            workspace_id=workspace.id,
            slug="order-older",
            title="Older Page",
            page_type="overview",
        )
        latest_page = ContextPage(
            workspace_id=workspace.id,
            slug="order-latest",
            title="Latest Page",
            page_type="overview",
        )
        session.add_all([older_page, latest_page])
        session.flush()

        def record(order):
            session_id = uuid4()
            chat = None
            for name in order:
                is_latest = name == "latest"
                if name == "cite":
                    tool_name = "cite"
                    input_summary = {"claim_text": "Older Page supports the claim"}
                    started_at = cited_at
                elif name == "search":
                    tool_name = "search"
                    input_summary = {"query": "later search must not beat authoring"}
                    started_at = search_at
                else:
                    tool_name = "update_draft" if is_latest else "write_draft"
                    input_summary = {"reason": name}
                    started_at = latest_at if is_latest else older_at
                chat, call = _record_chat_call(
                    service=service,
                    session=session,
                    workspace=workspace,
                    client=client,
                    mcp_session_id=session_id,
                    tool_name=tool_name,
                    input_summary=input_summary,
                    started_at=started_at,
                )
                if name != "search":
                    service.link_artifact(
                        session=session,
                        chat_session=chat,
                        tool_call=call,
                        resource_kind=McpChatArtifactKind.CONTEXT_PAGE,
                        resource_id=(latest_page.id if is_latest else older_page.id),
                        verb=(
                            McpChatArtifactVerb.EDITED
                            if is_latest
                            else (
                                McpChatArtifactVerb.CITED
                                if name == "cite"
                                else McpChatArtifactVerb.CREATED
                            )
                        ),
                    )
            return service.read_chat(
                session=session,
                workspace_id=workspace.id,
                chat_session_id=chat.id,
            )

        forward = record(("cite", "older", "latest", "search"))
        reverse = record(("search", "latest", "older", "cite"))

    expected = ("artifact", "edit", "Latest Page", "context_page")
    assert (
        forward.title_source,
        forward.action,
        forward.subject,
        forward.subject_kind,
    ) == expected
    assert (
        reverse.title_source,
        reverse.action,
        reverse.subject,
        reverse.subject_kind,
    ) == expected


def test_focused_read_requires_one_distinct_visible_artifact_across_the_chat(
    session_factory,
):
    service = McpChatService()
    with session_factory.begin() as session:
        workspace = session.scalar(select(Workspace).where(Workspace.slug == "default"))
        client = session.scalar(
            select(MCPClient).where(MCPClient.workspace_id == workspace.id)
        )
        first_page = ContextPage(
            workspace_id=workspace.id,
            slug="focused-read-first",
            title="Architecture Guide",
            page_type="guide",
        )
        second_page = ContextPage(
            workspace_id=workspace.id,
            slug="focused-read-second",
            title="Operations Guide",
            page_type="guide",
        )
        session.add_all([first_page, second_page])
        session.flush()

        focused_session_id = uuid4()
        focused_chat = None
        for _ in range(2):
            focused_chat, call = _record_chat_call(
                service=service,
                session=session,
                workspace=workspace,
                client=client,
                mcp_session_id=focused_session_id,
                tool_name="read_concept",
                input_summary={"path": "/context/architecture-guide.md"},
            )
            service.link_artifact(
                session=session,
                chat_session=focused_chat,
                tool_call=call,
                resource_kind=McpChatArtifactKind.CONTEXT_PAGE,
                resource_id=first_page.id,
                verb=McpChatArtifactVerb.REFERENCED,
            )
        focused = service.read_chat(
            session=session,
            workspace_id=workspace.id,
            chat_session_id=focused_chat.id,
        )

        ambiguous_session_id = uuid4()
        ambiguous_chat = None
        for page in (first_page, second_page):
            ambiguous_chat, ambiguous_call = _record_chat_call(
                service=service,
                session=session,
                workspace=workspace,
                client=client,
                mcp_session_id=ambiguous_session_id,
                tool_name="read_concept",
                input_summary={"path": f"/context/{page.slug}.md"},
            )
            service.link_artifact(
                session=session,
                chat_session=ambiguous_chat,
                tool_call=ambiguous_call,
                resource_kind=McpChatArtifactKind.CONTEXT_PAGE,
                resource_id=page.id,
                verb=McpChatArtifactVerb.REFERENCED,
            )
        assert ambiguous_chat is not None
        ambiguous = service.read_chat(
            session=session,
            workspace_id=workspace.id,
            chat_session_id=ambiguous_chat.id,
        )

    assert (
        focused.title_source,
        focused.action,
        focused.subject,
        focused.primary_tool_name,
    ) == ("artifact", "read", "Architecture Guide", "read_concept")
    assert ambiguous.title_source == "generic"
    assert ambiguous.subject is None


def test_bootstrap_only_and_allowlisted_inputs_never_infer_a_user_prompt(
    session_factory,
):
    service = McpChatService()
    with session_factory.begin() as session:
        workspace = session.scalar(select(Workspace).where(Workspace.slug == "default"))
        client = session.scalar(
            select(MCPClient).where(MCPClient.workspace_id == workspace.id)
        )
        bootstrap_id = uuid4()
        bootstrap_chat, _list_call = _record_chat_call(
            service=service,
            session=session,
            workspace=workspace,
            client=client,
            mcp_session_id=bootstrap_id,
            tool_name="list_my_workspaces",
            input_summary={},
            started_at=datetime(2026, 8, 5, 3, 1, tzinfo=UTC),
        )
        _chat, _guide_call = _record_chat_call(
            service=service,
            session=session,
            workspace=workspace,
            client=client,
            mcp_session_id=bootstrap_id,
            tool_name="guide",
            input_summary={"detail_level": "normal"},
            started_at=datetime(2026, 8, 5, 3, 0, tzinfo=UTC),
            result_summary='User asked: "leak this prompt"; 12 results',
        )
        bootstrap = service.read_chat(
            session=session,
            workspace_id=workspace.id,
            chat_session_id=bootstrap_chat.id,
        )

        cite_chat, _cite_call = _record_chat_call(
            service=service,
            session=session,
            workspace=workspace,
            client=client,
            mcp_session_id=uuid4(),
            tool_name="cite",
            input_summary={
                "claim_text": (
                    "  Cafe\u0301\trollout\u202e "
                    "https://docs.example/claim?code=CLAIM_SECRET#fragment "
                    "access_token=CLAIM_TOKEN  "
                )
            },
            status="partial",
        )
        cite = service.read_chat(
            session=session,
            workspace_id=workspace.id,
            chat_session_id=cite_chat.id,
        )

        grep_chat, _grep_call = _record_chat_call(
            service=service,
            session=session,
            workspace=workspace,
            client=client,
            mcp_session_id=uuid4(),
            tool_name="grep_wiki",
            input_summary={"pattern": "  OAuth\t연동  "},
        )
        grep = service.read_chat(
            session=session,
            workspace_id=workspace.id,
            chat_session_id=grep_chat.id,
        )

        safe_query_chat, _safe_query_call = _record_chat_call(
            service=service,
            session=session,
            workspace=workspace,
            client=client,
            mcp_session_id=uuid4(),
            tool_name="search",
            input_summary={
                "query": (
                    "ordinary query "
                    "https://auth.example/callback?code=QUERY_SECRET#fragment "
                    "token=QUERY_TOKEN "
                    "redirect_uri=https://redirect.example/private"
                )
            },
        )
        safe_query = service.read_chat(
            session=session,
            workspace_id=workspace.id,
            chat_session_id=safe_query_chat.id,
        )

        unsafe_chat, _unsafe_call = _record_chat_call(
            service=service,
            session=session,
            workspace=workspace,
            client=client,
            mcp_session_id=uuid4(),
            tool_name="custom_tool",
            input_summary={
                "title": "unallowlisted title",
                "query": "unallowlisted query",
                "claim_text": "unallowlisted claim",
                "prompt": "external prompt",
                "body_markdown": "secret body",
                "nested": {"title": "nested secret"},
            },
            result_summary="User requested unallowlisted result summary",
        )
        unsafe = service.read_chat(
            session=session,
            workspace_id=workspace.id,
            chat_session_id=unsafe_chat.id,
        )

    assert (
        bootstrap.title_source,
        bootstrap.action,
        bootstrap.subject,
        bootstrap.primary_tool_name,
    ) == ("tool", "guide", None, "guide")
    assert "leak this prompt" not in bootstrap.title
    assert (cite.title_source, cite.action, cite.subject) == (
        "input_text",
        "cite",
        "Café rollout https://docs.example/claim",
    )
    assert "CLAIM_SECRET" not in cite.subject
    assert "CLAIM_TOKEN" not in cite.subject
    assert (grep.title_source, grep.action, grep.subject) == (
        "query",
        "search",
        "OAuth 연동",
    )
    assert safe_query.title_source == "query"
    assert safe_query.subject == "ordinary query https://auth.example/callback"
    assert safe_query.intent_summary == safe_query.subject
    for secret in (
        "QUERY_SECRET",
        "QUERY_TOKEN",
        "fragment",
        "token=",
        "redirect_uri",
    ):
        assert secret not in safe_query.subject
    assert unsafe.title_source == "generic"
    assert unsafe.subject is None
    assert unsafe.title == "MCP activity"
    assert unsafe.summary == "MCP activity"
    assert unsafe.intent_summary is None


def test_hidden_authoring_event_does_not_change_visible_search_presentation(
    session_factory,
):
    service = McpChatService()
    hidden_title = "Invisible Acquisition Plan"
    with session_factory.begin() as session:
        workspace = session.scalar(select(Workspace).where(Workspace.slug == "default"))
        client = session.scalar(
            select(MCPClient).where(MCPClient.workspace_id == workspace.id)
        )
        hidden_page = ContextPage(
            workspace_id=workspace.id,
            slug="hidden-title-invariance",
            title=hidden_title,
            page_type="decision",
            lifecycle_status="archived",
        )
        session.add(hidden_page)
        session.flush()
        session_id = uuid4()
        chat, _search = _record_chat_call(
            service=service,
            session=session,
            workspace=workspace,
            client=client,
            mcp_session_id=session_id,
            tool_name="search",
            input_summary={"query": "public architecture"},
        )
        viewer = ActorContext(
            actor_kind="human",
            actor_id="viewer",
            token_kind="session",
            scopes=("read",),
            workspace_id=workspace.id,
            workspace_role="viewer",
            role="viewer",
        )
        before = service.read_chat(
            session=session,
            workspace_id=workspace.id,
            chat_session_id=chat.id,
            actor=viewer,
        )

        _chat, hidden_edit = _record_chat_call(
            service=service,
            session=session,
            workspace=workspace,
            client=client,
            mcp_session_id=session_id,
            tool_name="update_draft",
            input_summary={
                "title": hidden_title,
                "reason": f"Edit {hidden_title}",
                "body_markdown": "secret",
            },
            result_summary=f"Edited {hidden_title}; 1 result",
        )
        service.link_artifact(
            session=session,
            chat_session=chat,
            tool_call=hidden_edit,
            resource_kind=McpChatArtifactKind.CONTEXT_PAGE,
            resource_id=hidden_page.id,
            verb=McpChatArtifactVerb.EDITED,
        )
        after = service.read_chat(
            session=session,
            workspace_id=workspace.id,
            chat_session_id=chat.id,
            actor=viewer,
        )

    fields = (
        "title_source",
        "action",
        "subject",
        "subject_kind",
        "primary_tool_name",
    )
    assert tuple(getattr(before, field) for field in fields) == tuple(
        getattr(after, field) for field in fields
    )
    assert after.title_source == "query"
    assert after.subject == "public architecture"
    assert hidden_title not in after.title
    assert hidden_title not in after.summary
    assert hidden_title not in (after.intent_summary or "")

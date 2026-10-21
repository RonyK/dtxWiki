"""Chats detail projection of individual calls: who sees what of each row.

Covers the per-call gates (a failed call's named targets, a hidden search
match, the edited version), the edit previews and receipts those gates guard,
the cumulative session diff, and preview retention.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import event, select

from backend.models.audit import AuditEvent
from backend.models.context import ContextPage, ContextPageVersion
from backend.models.mcp_chat import McpChatArtifactKind, McpChatArtifactVerb
from backend.models.mcp_trace import MCPToolCall
from backend.models.user import User
from backend.models.workspace import MCPClient, Workspace
from backend.policy.auth import ActorContext
from backend.services.mcp_chat_service import (
    McpChatService,
    _ReaderGate,
    purge_previews_for_page,
)
from backend.services.mcp_trace_redaction import (
    redact_edit_receipt,
    redact_error_details,
    redact_tool_arguments,
)

BASE = datetime(2026, 9, 20, 9, 0, tzinfo=UTC)


class _Chat:
    """Build one chat's calls the way trace capture stores them."""

    def __init__(self, session, workspace, client, owner):
        self.session = session
        self.workspace = workspace
        self.client = client
        self.owner = owner
        self.mcp_session_id = uuid4()
        self.service = McpChatService()
        self.chat = None
        self._tick = 0

    def call(
        self,
        tool_name,
        arguments,
        *,
        status="succeeded",
        error_code=None,
        result_summary="Tool call succeeded.",
        result_detail=None,
        links=(),
    ):
        self._tick += 1
        started = BASE + timedelta(minutes=self._tick)
        call = MCPToolCall(
            client_id=self.client.id,
            workspace_id=self.workspace.id,
            mcp_session_id=self.mcp_session_id,
            request_id=uuid4(),
            tool_name=tool_name,
            input_summary=json.loads(
                json.dumps(redact_tool_arguments(tool_name, arguments))
            ),
            result_status=status,
            result_summary=result_summary,
            result_detail=result_detail,
            error_code=error_code,
            warnings=[],
            provider="unknown",
            model_id="unknown",
            params_summary={},
            client_self_reported=False,
            started_at=started,
            completed_at=started,
        )
        self.session.add(call)
        self.session.flush()
        self.chat = self.service.upsert_session_from_tool_call(
            session=self.session,
            workspace_id=self.workspace.id,
            client_id=self.client.id,
            mcp_session_id=str(self.mcp_session_id),
            tool_call=call,
            owner_user_id=self.owner.id,
        )
        for resource_kind, resource_id, verb in links:
            self.service.link_artifact(
                session=self.session,
                chat_session=self.chat,
                tool_call=call,
                resource_kind=resource_kind,
                resource_id=resource_id,
                verb=verb,
            )
        self.session.flush()
        return call

    def read(self, actor):
        return self.service.read_chat(
            session=self.session,
            workspace_id=self.workspace.id,
            chat_session_id=self.chat.id,
            actor=actor,
        )


def _setup(session):
    workspace = session.scalar(select(Workspace).where(Workspace.slug == "default"))
    client = session.scalar(
        select(MCPClient).where(MCPClient.workspace_id == workspace.id)
    )
    owner = User(
        email=f"chat-owner-{uuid4().hex[:8]}@example.com",
        password_hash="test-hash",
        display_name="Chat Owner",
    )
    session.add(owner)
    session.flush()
    return workspace, client, owner


def _human(user_id, workspace_id, role="editor") -> ActorContext:
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


def _page(session, workspace_id, slug, title, *, lifecycle_status="published"):
    page = ContextPage(
        workspace_id=workspace_id,
        slug=slug,
        title=title,
        page_type="overview",
        lifecycle_status=lifecycle_status,
    )
    session.add(page)
    session.flush()
    return page


def _version(session, page, *, status, author, number, parent=None):
    version = ContextPageVersion(
        workspace_id=page.workspace_id,
        page_id=page.id,
        version_no=number,
        parent_version_id=parent.id if parent is not None else None,
        body_md=f"# {page.title}\n\nbody {number}",
        content_hash=f"{number:064d}",
        actor_type="human",
        created_by_actor_kind="human",
        created_by_actor_id=str(author),
        version_status=status,
    )
    session.add(version)
    session.flush()
    if status == "published_snapshot":
        page.current_published_version_id = version.id
        page.current_version_id = version.id
    session.flush()
    return version


def _published_page(session, workspace_id, slug, title):
    page = _page(session, workspace_id, slug, title)
    published = _version(
        session, page, status="published_snapshot", author="seed", number=1
    )
    return page, published


def _patch_arguments(page_id, version_id, *, old_text="old words", new_text="new"):
    return {
        "workspace_id": str(uuid4()),
        "page_id": str(page_id),
        "version_id": str(version_id),
        "expected_editing_revision": 3,
        "operations": [
            {
                "op": "replace_text",
                "old_text": old_text,
                "new_text": new_text,
                "at": {"by": "heading_path", "heading_path": ["Guide", "Setup"]},
            },
            {"op": "insert_after", "block": "b_2", "content": "added paragraph"},
        ],
    }


def _item(detail, call):
    return next(entry for entry in detail.timeline if entry.id == call.id)


def _page_link(page, verb):
    return (McpChatArtifactKind.CONTEXT_PAGE, page.id, verb)


def test_input_version_number_resolves_exact_input_not_latest_or_result(
    session_factory,
):
    with session_factory.begin() as session:
        workspace, client, owner = _setup(session)
        page, original = _published_page(
            session, workspace.id, "version-input", "Guide"
        )
        original.version_status = "superseded_snapshot"
        session.flush()
        latest = _version(
            session, page, status="published_snapshot", author="seed", number=9
        )
        chat = _Chat(session, workspace, client, owner)
        call = chat.call(
            "update_context_edit",
            {"page_id": str(page.id), "version_id": str(original.id)},
            result_detail={"version_id": str(latest.id)},
            links=[_page_link(page, McpChatArtifactVerb.EDITED)],
        )
        item = _item(chat.read(_human(owner.id, workspace.id)), call)
        versions = [
            entry for entry in item.input_display_items if entry.key == "input_version"
        ]
        assert [(entry.text, entry.resource_id) for entry in versions] == [
            ("v1", original.id)
        ]


def test_input_version_number_does_not_expose_another_authors_private_version(
    session_factory,
):
    with session_factory.begin() as session:
        workspace, client, owner = _setup(session)
        page, _ = _published_page(
            session, workspace.id, "private-version-input", "Guide"
        )
        private = _version(
            session, page, status="editing", author=str(uuid4()), number=37
        )
        chat = _Chat(session, workspace, client, owner)
        call = chat.call(
            "update_context_edit",
            {"page_id": str(page.id), "version_id": str(private.id)},
            links=[_page_link(page, McpChatArtifactVerb.EDITED)],
        )
        item = _item(chat.read(_human(owner.id, workspace.id)), call)
        assert not any(
            entry.key == "input_version" for entry in item.input_display_items
        )
        assert "v37" not in item.model_dump_json()


def test_input_version_display_issues_no_queries_even_for_uncached_references(
    session_factory,
):
    with session_factory.begin() as session:
        workspace, client, owner = _setup(session)
        page, known = _published_page(session, workspace.id, "cached-version", "Guide")
        uncached = _version(session, page, status="draft", author=owner.id, number=2)
        chat = _Chat(session, workspace, client, owner)
        call = chat.call(
            "update_context_edit",
            {"version_id": str(known.id), "base_version_id": str(uncached.id)},
        )
        gate = _ReaderGate(
            chat.service, session, workspace.id, _human(owner.id, workspace.id)
        )
        assert gate.visible_version(known.id) is known
        statements = []
        connection = session.connection()

        def record(_conn, _cursor, statement, _parameters, _context, _many):
            statements.append(statement)

        event.listen(connection, "before_cursor_execute", record)
        try:
            items = gate.input_version_items(call)
        finally:
            event.remove(connection, "before_cursor_execute", record)
        assert [(entry.key, entry.text) for entry in items] == [("input_version", "v1")]
        assert statements == []
        assert uncached.id not in gate._versions


def test_missing_input_versions_use_one_metadata_query_for_repeated_calls(
    session_factory,
):
    with session_factory.begin() as session:
        workspace, client, owner = _setup(session)
        page, _ = _published_page(session, workspace.id, "batch-version", "Guide")
        versions = [
            _version(session, page, status="draft", author=owner.id, number=number)
            for number in range(2, 14)
        ]
        chat = _Chat(session, workspace, client, owner)
        calls = [
            chat.call("update_context_edit", {"version_id": str(version.id)})
            for version in versions
            for _ in range(2)
        ]
        gate = _ReaderGate(
            chat.service, session, workspace.id, _human(owner.id, workspace.id)
        )
        gate.title_and_path("context_page", page.id)
        statements = []
        connection = session.connection()

        def record(_conn, _cursor, statement, _parameters, _context, _many):
            statements.append(statement)

        event.listen(connection, "before_cursor_execute", record)
        try:
            gate.prime_input_versions(calls)
            for index, call in enumerate(calls):
                assert [entry.text for entry in gate.input_version_items(call)] == [
                    f"v{2 + index // 2}"
                ]
        finally:
            event.remove(connection, "before_cursor_execute", record)
        assert len(statements) == 1
        assert "body_md" not in statements[0]


# --- Failed calls (C1) ------------------------------------------------------


def test_failed_call_on_a_visible_target_shows_its_input_to_everyone(
    session_factory,
):
    """A failed call is no longer hidden wholesale. Its target is re-resolved
    for the reader; when the reader can see it, the input (including the edit
    previews) is shown. Only the server's message stays with the caller."""

    with session_factory.begin() as session:
        workspace, client, owner = _setup(session)
        page, published = _published_page(
            session, workspace.id, "failed-visible", "Failure Guide"
        )
        chat = _Chat(session, workspace, client, owner)
        call = chat.call(
            "patch_context_edit",
            _patch_arguments(page.id, published.id, old_text="missing sentence"),
            status="failed",
            error_code="text_not_found",
            result_summary="old_text not found near SERVER-MESSAGE-SECRET",
        )
        owner_view = _item(chat.read(_human(owner.id, workspace.id)), call)
        other_view = _item(chat.read(_human(uuid4(), workspace.id)), call)

    for view in (owner_view, other_view):
        assert view.error_code == "text_not_found"
        assert view.input_availability == "available"
        operations = view.input_summary["operations"]
        assert operations[0]["old_text_preview"]["text"] == "missing sentence"
        targets = [item for item in view.input_display_items if item.kind == "artifact"]
        assert [(item.key, item.text, item.resource_id) for item in targets] == [
            ("target_artifact", "Failure Guide", page.id)
        ]
        assert view.edit_summary is not None
        assert view.edit_summary.operation_counts == {
            "replace_text": 1,
            "insert_after": 1,
        }

    assert owner_view.redaction_reason is None
    assert owner_view.result_summary == (
        "old_text not found near SERVER-MESSAGE-SECRET"
    )
    assert other_view.redaction_reason == "error_restricted"
    assert other_view.result_summary is None
    assert other_view.result_availability == "redacted"
    assert "SERVER-MESSAGE-SECRET" not in other_view.model_dump_json()


def test_failed_call_naming_a_hidden_forged_or_foreign_target_is_one_shape(
    session_factory,
):
    """A UUID in a failed call's input is untrusted. Whether it names an
    archived page, nothing at all, or a page in another workspace, the reader
    gets the same shape-only projection -- no title, no id, no preview, no
    message -- so the projection cannot be used to probe for existence."""

    with session_factory.begin() as session:
        workspace, client, owner = _setup(session)
        archived = _page(
            session,
            workspace.id,
            "failed-archived",
            "ARCHIVED-TITLE-SECRET",
            lifecycle_status="archived",
        )
        other_workspace = Workspace(
            slug=f"other-{uuid4().hex[:8]}", title="Other", name="Other"
        )
        session.add(other_workspace)
        session.flush()
        foreign, _foreign_published = _published_page(
            session, other_workspace.id, "foreign-page", "FOREIGN-TITLE-SECRET"
        )
        chat = _Chat(session, workspace, client, owner)
        calls = [
            chat.call(
                "patch_context_edit",
                _patch_arguments(target, uuid4(), old_text="PREVIEW-SECRET"),
                status="conflict",
                error_code="stale_base_version",
                result_summary="MESSAGE-SECRET",
            )
            for target in (archived.id, uuid4(), foreign.id)
        ]
        detail = chat.read(_human(owner.id, workspace.id, role="owner"))

    views = [_item(detail, call) for call in calls]
    shapes = {
        json.dumps(
            view.model_dump(
                mode="json",
                include={
                    "headline",
                    "input_summary",
                    "input_availability",
                    "input_display_items",
                    "result_summary",
                    "result_availability",
                    "result_display_items",
                    "redaction_reason",
                    "error_code",
                    "edit_summary",
                    "session_diff",
                    "result_detail",
                    "linked_artifacts",
                },
            ),
            sort_keys=True,
        )
        for view in views
    }
    assert len(shapes) == 1
    view = views[0]
    assert view.redaction_reason == "target_hidden"
    assert view.error_code == "stale_base_version"
    assert view.headline == "patch context edit"
    assert view.result_summary is None
    assert view.input_summary == {
        "fields": [
            "expected_editing_revision",
            "operations",
            "page_id",
            "version_id",
            "workspace_id",
        ],
        "expected_editing_revision": 3,
        "operations": [{"op": "replace_text"}, {"op": "insert_after"}],
    }
    serialized = detail.model_dump_json()
    for secret in (
        "ARCHIVED-TITLE-SECRET",
        "FOREIGN-TITLE-SECRET",
        "PREVIEW-SECRET",
        "MESSAGE-SECRET",
        str(archived.id),
        str(foreign.id),
    ):
        assert secret not in serialized


def test_failed_call_error_details_are_structure_everyone_may_see(session_factory):
    """Which operations collided is the call's shape, like its error code: any
    reader of the row gets it. Line ranges -- and a refused text match's
    nearest-candidate lines -- describe the target's text, so a reader who
    cannot see the target gets the operation indexes only."""

    error_details = redact_error_details(
        [
            {
                "field": "operations[1]",
                "code": "operations_overlap",
                "message": (
                    "operations[0] (replace_text, lines 2-3) and operations[1] "
                    "(insert_after, line 3) target overlapping text: "
                    "partial overlap"
                ),
                "fix": "Merge the two operations.",
            },
            {
                "field": "operations[2].old_text",
                "code": "text_match_ambiguous",
                "message": "m",
                "operation_index": 2,
                "candidates": [
                    {
                        "line_start": 7,
                        "line_end": 7,
                        "block_id": "b_7",
                        "similarity": 0.97,
                        "match_kind": "fuzzy",
                    }
                ],
            },
        ]
    )
    with session_factory.begin() as session:
        workspace, client, owner = _setup(session)
        page, published = _published_page(
            session, workspace.id, "overlap-visible", "Overlap Visible"
        )
        archived = _page(
            session,
            workspace.id,
            "overlap-hidden",
            "Overlap Hidden",
            lifecycle_status="archived",
        )
        chat = _Chat(session, workspace, client, owner)
        visible_call, hidden_call = (
            chat.call(
                "patch_context_edit",
                _patch_arguments(target.id, version_id),
                status="failed",
                error_code="operations_overlap",
                result_summary="owner-only message",
                result_detail={"error_details": error_details},
            )
            for target, version_id in ((page, published.id), (archived, uuid4()))
        )
        detail = chat.read(_human(uuid4(), workspace.id))

    visible = _item(detail, visible_call)
    assert visible.redaction_reason == "error_restricted"
    assert [entry.model_dump() for entry in visible.error_details] == [
        {
            "code": "operations_overlap",
            "field": "operations[1]",
            "operation_index": 1,
            "conflict": {
                "left_op_index": 0,
                "right_op_index": 1,
                "left_lines": [2, 3],
                "right_lines": [3, 3],
                "overlap_kind": "partial",
            },
            "candidates": [],
        },
        {
            "code": "text_match_ambiguous",
            "field": "operations[2].old_text",
            "operation_index": 2,
            "conflict": None,
            "candidates": [
                {
                    "line_start": 7,
                    "line_end": 7,
                    "block_id": "b_7",
                    "similarity": 0.97,
                    "match_kind": "fuzzy",
                }
            ],
        },
    ]
    hidden = _item(detail, hidden_call)
    assert hidden.redaction_reason == "target_hidden"
    assert [entry.model_dump() for entry in hidden.error_details] == [
        {
            "code": "operations_overlap",
            "field": None,
            "operation_index": 1,
            "conflict": {
                "left_op_index": 0,
                "right_op_index": 1,
                "left_lines": None,
                "right_lines": None,
                "overlap_kind": "partial",
            },
            "candidates": [],
        },
        {
            "code": "text_match_ambiguous",
            "field": None,
            "operation_index": 2,
            "conflict": None,
            "candidates": [],
        },
    ]


def test_failed_call_without_target_ids_shows_only_its_shape(session_factory):
    with session_factory.begin() as session:
        workspace, client, owner = _setup(session)
        chat = _Chat(session, workspace, client, owner)
        call = chat.call(
            "create_context_page",
            {
                "workspace_id": str(workspace.id),
                "title": "SHAPE-TITLE-SECRET",
                "body_md": "# Draft\n\nSHAPE-BODY-SECRET",
            },
            status="failed",
            error_code="validation_failed",
            result_summary="title too long",
        )
        owner_view = _item(chat.read(_human(owner.id, workspace.id)), call)
        other_view = _item(chat.read(_human(uuid4(), workspace.id)), call)

    for view in (owner_view, other_view):
        assert view.input_summary == {
            "fields": ["body_md_digest", "body_md_preview", "title", "workspace_id"]
        }
        serialized = view.model_dump_json()
        assert "SHAPE-TITLE-SECRET" not in serialized
        assert "SHAPE-BODY-SECRET" not in serialized
        assert view.error_code == "validation_failed"
    assert owner_view.redaction_reason is None
    assert owner_view.result_summary == "title too long"
    assert other_view.redaction_reason == "error_restricted"
    assert other_view.result_summary is None


# --- Partial redaction (C2) -------------------------------------------------


def test_a_hidden_search_match_drops_only_that_result(session_factory):
    with session_factory.begin() as session:
        workspace, client, owner = _setup(session)
        visible, _published = _published_page(
            session, workspace.id, "match-visible", "Visible Match"
        )
        hidden = _page(
            session,
            workspace.id,
            "match-hidden",
            "HIDDEN-MATCH-SECRET",
            lifecycle_status="archived",
        )
        chat = _Chat(session, workspace, client, owner)
        search = chat.call(
            "search",
            {"query": "rollout checklist"},
            links=(
                _page_link(visible, McpChatArtifactVerb.SEARCH_MATCH),
                _page_link(hidden, McpChatArtifactVerb.SEARCH_MATCH),
            ),
        )
        edit = chat.call(
            "update_draft",
            {"title": "EDIT-INPUT-SECRET"},
            links=(
                _page_link(hidden, McpChatArtifactVerb.EDITED),
                _page_link(visible, McpChatArtifactVerb.SEARCH_MATCH),
            ),
        )
        detail = chat.read(_human(uuid4(), workspace.id))

    search_view = _item(detail, search)
    assert search_view.redaction_reason is None
    assert search_view.input_summary == {"query": "rollout checklist"}
    assert [item.resource_id for item in search_view.linked_artifacts] == [visible.id]
    assert detail.title_source == "query"
    assert detail.subject == "rollout checklist"

    edit_view = _item(detail, edit)
    assert edit_view.redaction_reason == "target_hidden"
    assert edit_view.input_summary == {}
    serialized = detail.model_dump_json()
    assert "HIDDEN-MATCH-SECRET" not in serialized
    assert "EDIT-INPUT-SECRET" not in serialized
    assert str(hidden.id) not in serialized


def test_the_callers_own_discarded_page_reads_as_a_discarded_edit(session_factory):
    with session_factory.begin() as session:
        workspace, client, owner = _setup(session)
        page = _page(
            session,
            workspace.id,
            "discarded-page",
            "DISCARDED-TITLE-SECRET",
            lifecycle_status="archived",
        )
        _version(session, page, status="discarded", author=owner.id, number=1)
        chat = _Chat(session, workspace, client, owner)
        call = chat.call(
            "create_context_page",
            {"workspace_id": str(workspace.id), "body_md": "# Scratch"},
            links=(_page_link(page, McpChatArtifactVerb.CREATED),),
        )
        owner_view = _item(chat.read(_human(owner.id, workspace.id)), call)
        other_view = _item(chat.read(_human(uuid4(), workspace.id)), call)

    assert owner_view.redaction_reason is None
    assert [
        (item.key, item.kind, item.resource_id)
        for item in owner_view.result_display_items
    ] == [("discarded_edit", "status", None)]
    assert owner_view.linked_artifacts == []
    assert "DISCARDED-TITLE-SECRET" not in owner_view.model_dump_json()
    assert str(page.id) not in owner_view.model_dump_json()
    assert other_view.redaction_reason == "target_hidden"


# --- Edit previews and receipts (C3/C4) ---------------------------------------


def test_previews_follow_the_visibility_of_the_edited_version(session_factory):
    """The page may be public while the edit went into its author's private
    editing row. The call stays visible to everyone who can see the page; the
    text it wrote is shown only to readers who can see that row."""

    with session_factory.begin() as session:
        workspace, client, owner = _setup(session)
        page, published = _published_page(
            session, workspace.id, "preview-gate", "Preview Gate"
        )
        editing = _version(
            session,
            page,
            status="editing",
            author=owner.id,
            number=2,
            parent=published,
        )
        chat = _Chat(session, workspace, client, owner)
        call = chat.call(
            "patch_context_edit",
            _patch_arguments(page.id, editing.id, new_text="PRIVATE-EDIT-TEXT"),
            links=(_page_link(page, McpChatArtifactVerb.EDITED),),
        )
        owner_view = _item(chat.read(_human(owner.id, workspace.id)), call)
        other_view = _item(chat.read(_human(uuid4(), workspace.id)), call)

    assert owner_view.input_summary["operations"][0]["new_text_preview"] == {
        "text": "PRIVATE-EDIT-TEXT",
        "truncated": False,
    }
    owner_previews = owner_view.edit_summary.operations[0].previews
    assert [(preview.field, preview.text) for preview in owner_previews] == [
        ("old_text", "old words"),
        ("new_text", "PRIVATE-EDIT-TEXT"),
    ]

    assert other_view.redaction_reason is None
    assert other_view.headline == "Edited: Preview Gate"
    assert "PRIVATE-EDIT-TEXT" not in other_view.model_dump_json()
    assert "old words" not in other_view.model_dump_json()
    other_operations = other_view.edit_summary.operations
    assert [operation.op for operation in other_operations] == [
        "replace_text",
        "insert_after",
    ]
    assert all(operation.previews == [] for operation in other_operations)
    assert other_view.edit_summary.previews_withheld is True
    assert owner_view.edit_summary.previews_withheld is False
    # The hidden-version projection is an allowlist that keeps no digests, so
    # there is nothing to estimate sizes from; only a server receipt's counts
    # would survive (none was recorded here).
    assert other_operations[0].chars_added is None
    assert other_operations[0].chars_removed is None
    for heading_or_block in ("Guide", "Setup", "b_2"):
        assert heading_or_block not in other_view.model_dump_json()


def test_a_hidden_version_projects_an_allowlist_not_a_denylist(session_factory):
    """When the reader cannot see the version a call wrote into, what remains
    of the call is built from a positive list of fields. A field nobody has
    heard of yet -- here one a client smuggled into an autosave -- is dropped,
    not passed through for want of a rule naming it."""

    receipt = redact_edit_receipt(
        {
            "edit_mode": "whole_body",
            "editing_revision_before": 2,
            "editing_revision_after": 3,
            "chars_added": 7,
            "chars_removed": 1,
            "lines_added": 1,
            "lines_removed": 0,
            "changed_sections": [["HIDDEN-SECTION"]],
            "first_change": {"line": 4, "before_preview": "a", "after_preview": "b"},
            "operations": [],
            "changed_blocks": [{"id": "b_hidden", "kind": "heading", "hash": "a" * 64}],
        }
    )
    with session_factory.begin() as session:
        workspace, client, owner = _setup(session)
        page, published = _published_page(
            session, workspace.id, "allowlist-page", "Allowlist Page"
        )
        editing = _version(
            session,
            page,
            status="editing",
            author=owner.id,
            number=2,
            parent=published,
        )
        chat = _Chat(session, workspace, client, owner)
        call = chat.call(
            "update_context_edit",
            {
                "workspace_id": str(workspace.id),
                "page_id": str(page.id),
                "base_version_id": str(editing.id),
                "expected_editing_revision": 2,
                "title": "HIDDEN-TITLE",
                "tags": ["HIDDEN-TAG"],
                "future_field": "FUTURE-FIELD-SECRET",
                "body_md": "# body",
            },
            result_detail={
                "page_id": str(page.id),
                "version_id": str(editing.id),
                "edit_receipt": receipt,
            },
            links=(_page_link(page, McpChatArtifactVerb.EDITED),),
        )
        other_view = _item(chat.read(_human(uuid4(), workspace.id)), call)
        owner_view = _item(chat.read(_human(owner.id, workspace.id)), call)

    assert other_view.input_summary == {
        "workspace_id": str(workspace.id),
        "page_id": str(page.id),
        "expected_editing_revision": 2,
    }
    assert other_view.result_detail.edit_receipt.model_dump() == {
        "edit_mode": "whole_body",
        "editing_revision_before": 2,
        "editing_revision_after": 3,
        "chars_added": 7,
        "chars_removed": 1,
        "lines_added": 1,
        "lines_removed": 0,
        "changed_sections": [],
        "first_change": None,
        "operations": [],
        "changed_blocks": [],
    }
    serialized = other_view.model_dump_json()
    for secret in ("HIDDEN", "FUTURE-FIELD-SECRET", "b_hidden"):
        assert secret not in serialized
    assert other_view.headline == "Edited: Allowlist Page"
    assert "FUTURE-FIELD-SECRET" in owner_view.model_dump_json()
    assert "HIDDEN-TITLE" in owner_view.model_dump_json()


def test_context_edit_inputs_never_feed_chat_labels():
    """Titles, summaries, intents and list search read call inputs only
    through these allowlists; a context-edit call's input (a private edit's
    title, tags, reason) must never be one of them."""

    from backend.services import mcp_chat_service as module

    label_tools = (
        set(module._SAFE_TEXT_INPUT_FIELDS)
        | set(module._SAFE_KIND_INPUT_FIELDS)
        | set(module._SEARCH_INPUT_FIELDS)
    )
    assert module.CONTEXT_EDIT_TOOLS.isdisjoint(label_tools)


def test_a_discarded_editing_row_stays_its_authors_alone(session_factory):
    """Discarding closes an editing row as "discarded", which the generic
    draft rule would show to every editor. The text written into it was
    private while it was open and was then thrown away, so Chats keeps it
    with its author."""

    with session_factory.begin() as session:
        workspace, client, owner = _setup(session)
        page, published = _published_page(
            session, workspace.id, "discarded-row", "Discarded Row"
        )
        discarded = _version(
            session,
            page,
            status="discarded",
            author=owner.id,
            number=2,
            parent=published,
        )
        chat = _Chat(session, workspace, client, owner)
        call = chat.call(
            "patch_context_edit",
            _patch_arguments(page.id, discarded.id, new_text="THROWN-AWAY-TEXT"),
            links=(_page_link(page, McpChatArtifactVerb.EDITED),),
        )
        owner_view = _item(chat.read(_human(owner.id, workspace.id)), call)
        other_view = _item(chat.read(_human(uuid4(), workspace.id)), call)

    assert owner_view.edit_summary.previews_withheld is False
    assert "THROWN-AWAY-TEXT" in owner_view.model_dump_json()
    assert other_view.redaction_reason is None
    assert other_view.edit_summary.previews_withheld is True
    assert "THROWN-AWAY-TEXT" not in other_view.model_dump_json()


def test_edit_summary_estimates_legacy_calls_from_their_digests(session_factory):
    with session_factory.begin() as session:
        workspace, client, owner = _setup(session)
        page, published = _published_page(
            session, workspace.id, "legacy-digests", "Legacy Digests"
        )
        chat = _Chat(session, workspace, client, owner)
        call = chat.call(
            "patch_context_edit",
            {
                "page_id": str(page.id),
                "version_id": str(published.id),
                "operations": [
                    {"op": "replace_text", "old_text": "abc", "new_text": "abcdef"},
                    {
                        "op": "replace_section",
                        "at": {"by": "section_id", "section_id": "s1"},
                        "content": "1234",
                    },
                ],
            },
            links=(_page_link(page, McpChatArtifactVerb.EDITED),),
        )
        stored = session.get(MCPToolCall, call.id)
        # Calls captured before previews existed carry digests only.
        stored.input_summary = {
            **stored.input_summary,
            "operations": [
                {
                    key: value
                    for key, value in operation.items()
                    if not key.endswith("_preview")
                }
                for operation in stored.input_summary["operations"]
            ],
        }
        session.flush()
        view = _item(chat.read(_human(owner.id, workspace.id)), call)

    summary = view.edit_summary
    assert summary.source == "input"
    assert [
        (operation.op, operation.chars_added, operation.chars_removed)
        for operation in summary.operations
    ] == [("replace_text", 6, 3), ("replace_section", 4, None)]
    assert summary.chars_added == 10
    assert summary.chars_removed is None
    assert summary.changed_sections == []
    assert {item.key: item.text for item in view.input_display_items}[
        "edit_operations"
    ] == "replace_text ×1, replace_section ×1"


def test_edit_summary_and_result_detail_use_the_server_receipt(session_factory):
    receipt = redact_edit_receipt(
        {
            "edit_mode": "whole_body",
            "editing_revision_before": 4,
            "editing_revision_after": 5,
            "chars_added": 40,
            "chars_removed": 12,
            "lines_added": 2,
            "lines_removed": 1,
            "changed_sections": [["Guide", "Setup"], ["FAQ"]],
            "first_change": {
                "line": 9,
                "before_preview": "old sentence",
                "after_preview": "new sentence",
            },
            "operations": [],
        }
    )
    with session_factory.begin() as session:
        workspace, client, owner = _setup(session)
        page, published = _published_page(
            session, workspace.id, "receipt-page", "Receipt Page"
        )
        chat = _Chat(session, workspace, client, owner)
        call = chat.call(
            "update_context_edit",
            {
                "page_id": str(page.id),
                "base_version_id": str(published.id),
                "expected_editing_revision": 4,
                "title": "Receipt Page v2",
                "page_type": "guide",
                "body_md": "# Receipt Page\n\nwhole new body",
            },
            result_detail={"edit_receipt": receipt},
            links=(_page_link(page, McpChatArtifactVerb.EDITED),),
        )
        view = _item(chat.read(_human(owner.id, workspace.id)), call)

    summary = view.edit_summary
    assert summary.source == "receipt"
    assert summary.edit_mode == "whole_body"
    assert (summary.chars_added, summary.chars_removed) == (40, 12)
    assert (summary.lines_added, summary.lines_removed) == (2, 1)
    assert summary.changed_sections == [["Guide", "Setup"], ["FAQ"]]
    assert summary.first_change.before_preview == "old sentence"
    assert summary.body_preview is None
    assert view.result_detail.edit_receipt.editing_revision_after == 5
    items = {item.key: item.text for item in view.input_display_items}
    assert items["context_page"] == "Receipt Page v2"
    assert items["page_type"] == "guide"
    assert items["edit_sections"] == "Guide › Setup; FAQ"
    assert items["edit_chars"] == "+40 −12"


def test_list_search_never_matches_preview_text(session_factory):
    """Previews are durable columns that cannot follow later visibility
    changes, so they are never part of what list search or labels read."""

    with session_factory.begin() as session:
        workspace, client, owner = _setup(session)
        page, published = _published_page(
            session, workspace.id, "search-preview", "Search Preview"
        )
        chat = _Chat(session, workspace, client, owner)
        chat.call(
            "patch_context_edit",
            _patch_arguments(page.id, published.id, new_text="PREVIEW-NEEDLE-7731"),
            links=(_page_link(page, McpChatArtifactVerb.EDITED),),
        )
        service = chat.service
        ungated = service.list_chats(
            session=session, workspace_id=workspace.id, q="PREVIEW-NEEDLE-7731"
        )
        gated = service.list_chats(
            session=session,
            workspace_id=workspace.id,
            q="PREVIEW-NEEDLE-7731",
            actor=_human(owner.id, workspace.id),
        )
        listed = service.list_chats(
            session=session,
            workspace_id=workspace.id,
            actor=_human(owner.id, workspace.id),
        )
        stored = chat.chat

    assert ungated.items == []
    assert gated.items == []
    assert "PREVIEW-NEEDLE-7731" not in (stored.title or "")
    assert "PREVIEW-NEEDLE-7731" not in (stored.summary or "")
    assert "PREVIEW-NEEDLE-7731" not in listed.model_dump_json()


def test_detail_and_export_project_every_call_identically(session_factory):
    with session_factory.begin() as session:
        workspace, client, owner = _setup(session)
        page, published = _published_page(
            session, workspace.id, "parity-page", "Parity Page"
        )
        archived = _page(
            session,
            workspace.id,
            "parity-hidden",
            "Hidden",
            lifecycle_status="archived",
        )
        editing = _version(
            session,
            page,
            status="editing",
            author=owner.id,
            number=2,
            parent=published,
        )
        chat = _Chat(session, workspace, client, owner)
        chat.call(
            "patch_context_edit",
            _patch_arguments(page.id, editing.id),
            links=(_page_link(page, McpChatArtifactVerb.EDITED),),
        )
        chat.call(
            "patch_context_edit",
            _patch_arguments(page.id, published.id),
            status="failed",
            error_code="text_not_found",
            result_summary="owner-only message",
        )
        chat.call(
            "patch_context_edit",
            _patch_arguments(archived.id, uuid4()),
            status="conflict",
            error_code="stale_base_version",
        )
        chat.call(
            "search",
            {"query": "parity"},
            links=(
                _page_link(page, McpChatArtifactVerb.SEARCH_MATCH),
                _page_link(archived, McpChatArtifactVerb.SEARCH_MATCH),
            ),
        )
        for actor in (
            _human(owner.id, workspace.id),
            _human(uuid4(), workspace.id),
            _human(uuid4(), workspace.id, role="viewer"),
        ):
            detail = chat.read(actor)
            exported = chat.service.export_chats(
                session=session, workspace_id=workspace.id, actor=actor
            )
            exported_chat = next(
                item for item in exported.chats if item.id == chat.chat.id
            )
            assert [entry.model_dump() for entry in exported_chat.timeline] == [
                entry.model_dump() for entry in detail.timeline
            ]


# --- Cumulative session diff --------------------------------------------------


def test_session_diff_is_offered_on_submit_and_on_the_open_sessions_last_edit(
    session_factory,
):
    with session_factory.begin() as session:
        workspace, client, owner = _setup(session)
        page, published = _published_page(
            session, workspace.id, "diff-open", "Diff Open"
        )
        editing = _version(
            session,
            page,
            status="editing",
            author=owner.id,
            number=2,
            parent=published,
        )
        submitted_page, submitted_base = _published_page(
            session, workspace.id, "diff-submitted", "Diff Submitted"
        )
        draft = _version(
            session,
            submitted_page,
            status="draft",
            author=owner.id,
            number=2,
            parent=submitted_base,
        )
        new_page = _page(
            session, workspace.id, "diff-new", "Diff New", lifecycle_status="draft"
        )
        new_editing = _version(
            session, new_page, status="editing", author=owner.id, number=1
        )
        new_page.current_version_id = new_editing.id
        session.flush()
        chat = _Chat(session, workspace, client, owner)
        first = chat.call(
            "patch_context_edit",
            _patch_arguments(page.id, editing.id),
            links=(_page_link(page, McpChatArtifactVerb.EDITED),),
        )
        last = chat.call(
            "patch_context_edit",
            _patch_arguments(page.id, editing.id),
            links=(_page_link(page, McpChatArtifactVerb.EDITED),),
        )
        submit = chat.call(
            "submit_context_edit_for_review",
            {
                "page_id": str(submitted_page.id),
                "version_id": str(draft.id),
                "expected_editing_revision": 2,
                "reason": "ready for review",
            },
            links=(_page_link(submitted_page, McpChatArtifactVerb.EDITED),),
        )
        created = chat.call(
            "create_context_page",
            {"workspace_id": str(workspace.id), "title": "Diff New", "body_md": "x"},
            result_detail={"version_id": str(new_editing.id)},
            links=(_page_link(new_page, McpChatArtifactVerb.CREATED),),
        )
        owner_detail = chat.read(_human(owner.id, workspace.id))
        other_detail = chat.read(_human(uuid4(), workspace.id))

    assert _item(owner_detail, first).session_diff is None
    last_diff = _item(owner_detail, last).session_diff
    assert (
        last_diff.page_id,
        last_diff.from_version_id,
        last_diff.to_version_id,
    ) == (page.id, published.id, editing.id)
    submit_diff = _item(owner_detail, submit).session_diff
    assert (submit_diff.from_version_id, submit_diff.to_version_id) == (
        submitted_base.id,
        draft.id,
    )
    created_diff = _item(owner_detail, created).session_diff
    assert (created_diff.from_version_id, created_diff.to_version_id) == (
        None,
        new_editing.id,
    )
    assert {item.key for item in _item(owner_detail, submit).input_display_items} >= {
        "write_reason"
    }

    # Someone else's open editing row is private; their submitted draft is not.
    assert _item(other_detail, last).session_diff is None
    assert _item(other_detail, submit).session_diff is not None


# --- Retention ----------------------------------------------------------------


def _preview_texts(value):
    found = []

    def visit(node):
        if isinstance(node, dict):
            for key, item in node.items():
                if key.endswith("_preview"):
                    found.append(item)
                else:
                    visit(item)
        elif isinstance(node, list):
            for item in node:
                visit(item)

    visit(value)
    return found


def test_owner_purge_nulls_a_chats_previews_and_is_audited(session_factory):
    receipt = redact_edit_receipt(
        {
            "edit_mode": "patch",
            "editing_revision_before": 1,
            "editing_revision_after": 2,
            "first_change": {"line": 1, "before_preview": "a", "after_preview": "b"},
            "operations": [],
        }
    )
    with session_factory.begin() as session:
        workspace, client, owner = _setup(session)
        page, published = _published_page(
            session, workspace.id, "purge-chat", "Purge Chat"
        )
        chat = _Chat(session, workspace, client, owner)
        patch = chat.call(
            "patch_context_edit",
            _patch_arguments(page.id, published.id),
            result_detail={"version_id": str(published.id), "edit_receipt": receipt},
            links=(_page_link(page, McpChatArtifactVerb.EDITED),),
        )
        chat.call("search", {"query": "no previews here"})
        actor = _human(owner.id, workspace.id, role="owner")
        result = chat.service.purge_chat_previews(
            session=session,
            workspace_id=workspace.id,
            chat_session_id=chat.chat.id,
            actor=actor,
        )
        stored = session.get(MCPToolCall, patch.id)
        view = _item(chat.read(actor), patch)
        audit = session.get(AuditEvent, result.audit_event_id)

    assert result.purged_call_count == 1
    assert _preview_texts(stored.input_summary) == [None, None, None]
    assert stored.input_summary["operations"][0]["old_text_digest"]["chars"] == 9
    assert stored.result_detail["edit_receipt"]["first_change"] == {
        "line": 1,
        "before_preview": None,
        "after_preview": None,
    }
    assert view.edit_summary.previews_purged is True
    assert [
        (preview.field, preview.text)
        for preview in view.edit_summary.operations[0].previews
    ] == [("old_text", None), ("new_text", None)]
    assert audit.action == "mcp_chat_session.previews_purged"
    assert audit.target_id == chat.chat.id


def test_discard_purge_is_scoped_to_the_discarded_version(session_factory):
    with session_factory.begin() as session:
        workspace, client, owner = _setup(session)
        page, published = _published_page(
            session, workspace.id, "purge-page", "Purge Page"
        )
        chat = _Chat(session, workspace, client, owner)
        discarded_id, kept_id = uuid4(), uuid4()
        on_discarded = chat.call(
            "patch_context_edit",
            _patch_arguments(page.id, discarded_id),
            links=(_page_link(page, McpChatArtifactVerb.EDITED),),
        )
        failed_on_discarded = chat.call(
            "patch_context_edit",
            _patch_arguments(page.id, discarded_id),
            status="failed",
        )
        created_discarded = chat.call(
            "create_context_page",
            {"workspace_id": str(workspace.id), "body_md": "# New\n\ncreated body"},
            result_detail={
                "page_id": str(page.id),
                "version_id": str(discarded_id),
            },
            links=(_page_link(page, McpChatArtifactVerb.CREATED),),
        )
        on_kept = chat.call(
            "patch_context_edit",
            _patch_arguments(page.id, kept_id),
            links=(_page_link(page, McpChatArtifactVerb.EDITED),),
        )
        purged = purge_previews_for_page(
            session, workspace.id, page.id, version_id=discarded_id
        )
        states = {
            call.id: _preview_texts(session.get(MCPToolCall, call.id).input_summary)
            for call in (on_discarded, failed_on_discarded, created_discarded, on_kept)
        }
        purge_previews_for_page(session, workspace.id, page.id)
        after_page_purge = _preview_texts(
            session.get(MCPToolCall, on_kept.id).input_summary
        )

    assert purged == 3
    assert all(
        text is None
        for call in (on_discarded, failed_on_discarded, created_discarded)
        for text in states[call.id]
    )
    assert all(text is not None for text in states[on_kept.id])
    assert after_page_purge == [None, None, None]


# --- Counters (C6) ------------------------------------------------------------


def test_context_edit_tools_count_as_writes(session_factory):
    with session_factory.begin() as session:
        workspace, client, owner = _setup(session)
        chat = _Chat(session, workspace, client, owner)
        for tool_name in (
            "create_context_page",
            "begin_context_edit",
            "update_context_edit",
            "patch_context_edit",
            "submit_context_edit_for_review",
            "discard_context_edit",
        ):
            chat.call(tool_name, {"workspace_id": str(workspace.id)})
        chat.call("search", {"query": "not a write"})
        session.refresh(chat.chat)
        write_count = chat.chat.write_count
        writes_only = chat.service.list_chats(
            session=session, workspace_id=workspace.id, writes_only=True
        )

    assert write_count == 6
    assert UUID(str(writes_only.items[0].id)) == chat.chat.id

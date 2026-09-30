"""Codex #11: submit_revision_for_review trace links the revised page for BOTH
the `update` and the `create` revision sources (was: only `update.page_id`)."""

import base64
import json
from uuid import uuid4

import pytest
from sqlalchemy import select

from backend.models.change import ChangeSet
from backend.models.context import ContextPage, ContextPageVersion
from backend.models.mcp_chat import McpChatArtifactKind, McpChatArtifactVerb
from backend.models.workspace import Workspace
from backend.schemas.common import MCPResultEnvelope
from backend.schemas.mcp.upload_tools import (
    UploadImageResult,
    UploadImageToolOutput,
)
from backend.services.mcp_trace_capture import derive_artifact_links
from backend.services.mcp_trace_redaction import redact_tool_capture


def _seed_change_set_with_page(session, workspace_id):
    change_set = ChangeSet(
        workspace_id=workspace_id, trigger_type="mcp", actor_type="human"
    )
    session.add(change_set)
    session.flush()
    page = ContextPage(
        workspace_id=workspace_id,
        slug="revised-page",
        title="Revised Page",
        page_type="overview",
    )
    session.add(page)
    session.flush()
    version = ContextPageVersion(
        workspace_id=workspace_id,
        page_id=page.id,
        version_no=1,
        body_md="revised body",
        content_hash="h-revised",
        change_set_id=change_set.id,
        actor_type="human",
        created_by_actor_kind="human",
        created_by_actor_id="tester",
    )
    session.add(version)
    session.flush()
    return change_set, page


def _page_links(candidates):
    return [
        c for c in candidates if c.resource_kind == McpChatArtifactKind.CONTEXT_PAGE
    ]


def test_submit_revision_create_links_new_page(page_service):
    with page_service.session_factory.begin() as session:
        ws = session.scalar(select(Workspace).where(Workspace.slug == "default"))
        change_set, page = _seed_change_set_with_page(session, ws.id)
        candidates = derive_artifact_links(
            session=session,
            tool_name="submit_revision_for_review",
            arguments={
                "create": {
                    "title": "Revised Page",
                    "page_type": "overview",
                    "body_markdown": "revised body",
                    "reason": "revision via create",
                }
            },
            payload={"data": {"new_change_set_id": str(change_set.id)}},
        )
    links = _page_links(candidates)
    assert len(links) == 1
    assert links[0].resource_id == page.id
    assert links[0].verb == McpChatArtifactVerb.CREATED


def test_submit_revision_update_links_page(page_service):
    with page_service.session_factory.begin() as session:
        ws = session.scalar(select(Workspace).where(Workspace.slug == "default"))
        change_set, page = _seed_change_set_with_page(session, ws.id)
        candidates = derive_artifact_links(
            session=session,
            tool_name="submit_revision_for_review",
            arguments={
                "update": {
                    "page_id": str(page.id),
                    "body_markdown": "revised body",
                    "reason": "revision via update",
                }
            },
            payload={"data": {"new_change_set_id": str(change_set.id)}},
        )
    links = _page_links(candidates)
    assert len(links) == 1
    assert links[0].resource_id == page.id
    assert links[0].verb == McpChatArtifactVerb.EDITED


def test_read_concept_links_referenced_page(page_service):
    with page_service.session_factory.begin() as session:
        ws = session.scalar(select(Workspace).where(Workspace.slug == "default"))
        page = ContextPage(
            workspace_id=ws.id,
            slug="concept-trace",
            title="Concept Trace",
            page_type="concept",
        )
        session.add(page)
        session.flush()
        candidates = derive_artifact_links(
            session=session,
            tool_name="read_concept",
            arguments={
                "workspace_id": str(ws.id),
                "path": "/context/concept-trace.md",
            },
            payload={
                "status": "found",
                "page_id": str(page.id),
                "version_id": "00000000-0000-0000-0000-000000000001",
            },
        )

    links = _page_links(candidates)
    assert len(links) == 1
    assert links[0].resource_id == page.id
    assert links[0].verb == McpChatArtifactVerb.REFERENCED


def test_grep_wiki_links_search_matched_pages(page_service):
    with page_service.session_factory.begin() as session:
        ws = session.scalar(select(Workspace).where(Workspace.slug == "default"))
        page = ContextPage(
            workspace_id=ws.id,
            slug="grep-trace",
            title="Grep Trace",
            page_type="concept",
        )
        session.add(page)
        session.flush()
        candidates = derive_artifact_links(
            session=session,
            tool_name="grep_wiki",
            arguments={"workspace_id": str(ws.id), "pattern": "needle"},
            payload={
                "matches": [
                    {"page_id": str(page.id), "line": "needle one"},
                    {"page_id": str(page.id), "line": "needle two"},
                ]
            },
        )

    links = _page_links(candidates)
    assert len(links) == 1  # deduplicated per page
    assert links[0].resource_id == page.id
    assert links[0].verb == McpChatArtifactVerb.SEARCH_MATCH


@pytest.mark.parametrize(
    "tool_name",
    [
        "read",
        "read_page",
        "read_agent_page",
        "fetch",
        "read_concept",
        "read_context_outline",
        "read_structure_map",
        "begin_context_edit",
    ],
)
def test_focused_read_tools_still_derive_referenced_links(page_service, tool_name):
    page_id = uuid4()
    version_id = uuid4()
    arguments: dict[str, object] = {}
    payload: dict[str, object] = {}
    if tool_name in {"read", "read_page"}:
        arguments = (
            {"target_type": "context_page", "target_id": str(page_id)}
            if tool_name == "read"
            else {"page_id": str(page_id)}
        )
    elif tool_name == "read_agent_page":
        arguments = {"agent_page_id": str(page_id)}
    elif tool_name == "fetch":
        arguments = {"type": "context_page", "id": str(page_id)}
    elif tool_name == "read_concept":
        payload = {"page_id": str(page_id), "version_id": str(version_id)}
    elif tool_name in {"read_context_outline", "read_structure_map"}:
        payload = {
            "document": {
                "page_id": str(page_id),
                "version_id": str(version_id),
                "content_hash": "a" * 64,
            }
        }
    else:
        payload = {"data": {"id": str(page_id), "version_id": str(version_id)}}

    with page_service.session_factory() as session:
        links = derive_artifact_links(
            session=session,
            tool_name=tool_name,
            arguments=arguments,
            payload=payload,
        )

    assert links
    assert {link.verb for link in links} == {McpChatArtifactVerb.REFERENCED}
    if tool_name in {
        "read_context_outline",
        "read_structure_map",
        "begin_context_edit",
    }:
        assert links[0].version_id == version_id


def test_patch_context_edit_links_the_edited_page(page_service):
    version_id = uuid4()
    content_hash = "a" * 64
    with page_service.session_factory.begin() as session:
        ws = session.scalar(select(Workspace).where(Workspace.slug == "default"))
        page = ContextPage(
            workspace_id=ws.id,
            slug="patch-trace",
            title="Patch Trace",
            page_type="concept",
        )
        session.add(page)
        session.flush()
        candidates = derive_artifact_links(
            session=session,
            tool_name="patch_context_edit",
            arguments={"workspace_id": str(ws.id), "page_id": str(page.id)},
            payload={
                "data": {
                    "id": str(page.id),
                    "version_id": str(version_id),
                    "content_hash": content_hash,
                }
            },
        )

    links = _page_links(candidates)
    assert len(links) == 1
    assert links[0].resource_id == page.id
    assert links[0].verb == McpChatArtifactVerb.EDITED
    assert links[0].version_id == version_id
    assert links[0].root_hash == content_hash


def test_read_context_outline_links_the_resolved_page(page_service):
    with page_service.session_factory.begin() as session:
        ws = session.scalar(select(Workspace).where(Workspace.slug == "default"))
        page = ContextPage(
            workspace_id=ws.id,
            slug="outline-trace",
            title="Outline Trace",
            page_type="concept",
        )
        session.add(page)
        session.flush()
        candidates = derive_artifact_links(
            session=session,
            tool_name="read_context_outline",
            # Addressed by path: only the response knows which page it resolved to.
            arguments={"workspace_id": str(ws.id), "target": {"path": "context/x"}},
            # ReadContextOutlineOutput is flat -- document lives at the
            # payload's top level, not under a "data" wrapper like the write
            # tools' outputs.
            payload={"document": {"page_id": str(page.id)}},
        )

    links = _page_links(candidates)
    assert len(links) == 1
    assert links[0].resource_id == page.id
    assert links[0].verb == McpChatArtifactVerb.REFERENCED


def test_upload_image_links_the_created_asset_page_bound(page_service):
    """upload_image, always bound to an existing page_id and the caller's own
    open editing_version_id under the page-first contract, links
    CONTEXT_MEDIA_ASSET keyed by the response's asset_id (not the page_id
    argument) -- the asset id is what stays resolvable across a later claim
    (Phase 5a)."""

    with page_service.session_factory.begin() as session:
        ws = session.scalar(select(Workspace).where(Workspace.slug == "default"))
        page = ContextPage(
            workspace_id=ws.id,
            slug="upload-trace-page",
            title="Upload Trace Page",
            page_type="concept",
        )
        session.add(page)
        session.flush()
        asset_id = uuid4()
        editing_version_id = uuid4()
        candidates = derive_artifact_links(
            session=session,
            tool_name="upload_image",
            arguments={
                "workspace_id": str(ws.id),
                "page_id": str(page.id),
                "editing_version_id": str(editing_version_id),
                "filename": "diagram.png",
                "mime_type": "image/png",
                "marker_uuid": str(asset_id),
            },
            payload={
                "data": {
                    "asset_id": str(asset_id),
                    "logical_ref": f"dtx-media://asset/{asset_id}",
                    "reused": False,
                }
            },
        )

    assert len(candidates) == 1
    assert candidates[0].resource_kind == McpChatArtifactKind.CONTEXT_MEDIA_ASSET
    assert candidates[0].resource_id == asset_id
    assert candidates[0].verb == McpChatArtifactVerb.CREATED


def test_upload_image_edge_survives_the_real_response_and_redaction(page_service):
    """The two tests above hand-write the payload dict, so they would stay
    green even if the real response never carried `asset_id` where the
    derivation looks for it.  This one starts from the actual
    ``UploadImageToolOutput`` model, serializes it the way
    ``mcp/protocol/runner.py:243`` does (``model_dump(mode="json")``), and
    puts it through ``redact_tool_capture`` -- the only two transforms
    standing between the handler's return value and
    ``derive_artifact_links`` -- before asserting the edge.

    Redaction is the part worth pinning: ``upload_image`` is the one tool with
    a payload-scrubbing rule (``mcp_trace_redaction.py:413``), and it rewrites
    the response dict.  A future widening of that scrub could silently erase
    the id this edge is keyed by.
    """

    asset_id = uuid4()
    workspace_id = uuid4()
    page_id = uuid4()
    editing_version_id = uuid4()
    content_base64 = base64.b64encode(b"not-a-real-png").decode("ascii")
    response = UploadImageToolOutput(
        envelope=MCPResultEnvelope(
            result_status="succeeded",
            workspace_id=workspace_id,
            actor_kind="human",
            actor_id="tester",
            result_summary="Tool call succeeded.",
        ),
        data=UploadImageResult(
            asset_id=asset_id,
            logical_ref=f"dtx-media://asset/{asset_id}",
            content_url=(
                f"/api/v1/workspaces/{workspace_id}/context/pages/{page_id}"
                f"/versions/{editing_version_id}/media/{asset_id}/content"
            ),
            markdown=f"![diagram](dtx-media://asset/{asset_id})",
            mime_type="image/png",
            bytes=14,
            sha256="a" * 64,
            marker_uuid=uuid4(),
            reused=False,
        ),
    )
    arguments = {
        "page_id": str(page_id),
        "editing_version_id": str(editing_version_id),
        "filename": "diagram.png",
        "mime_type": "image/png",
        "content_base64": content_base64,
    }
    _, traced_payload = redact_tool_capture(
        "upload_image", arguments, response.model_dump(mode="json")
    )

    with page_service.session_factory.begin() as session:
        candidates = derive_artifact_links(
            session=session,
            tool_name="upload_image",
            arguments=arguments,
            payload=traced_payload,
        )

    assert content_base64 not in json.dumps(traced_payload)
    assert len(candidates) == 1
    assert candidates[0].resource_kind == McpChatArtifactKind.CONTEXT_MEDIA_ASSET
    assert candidates[0].resource_id == asset_id
    assert candidates[0].verb == McpChatArtifactVerb.CREATED


def test_every_tool_is_edge_deriving_or_declared_edge_less():
    """§5.6 registry contract: a new MCP tool must either derive usage edges
    in derive_artifact_links or be explicitly declared edge-less — silence
    is a test failure, not a blind spot."""

    from backend.mcp.router import TOOL_HANDLERS
    from backend.services.mcp_trace_capture import (
        EDGE_DERIVING_TOOLS,
        EDGE_LESS_TOOLS,
    )

    tools = set(TOOL_HANDLERS)
    assert EDGE_DERIVING_TOOLS & EDGE_LESS_TOOLS == set()
    unregistered = tools - EDGE_DERIVING_TOOLS - EDGE_LESS_TOOLS
    assert unregistered == set(), (
        f"tools missing an edge-derivation decision: {sorted(unregistered)}"
    )


def _edit_trace_payload(workspace_id, *, data, result_status="succeeded"):
    return {
        "envelope": {
            "result_status": result_status,
            "workspace_id": str(workspace_id),
            "request_id": str(uuid4()),
            "result_summary": "Tool call succeeded.",
        },
        "data": data,
    }


def _persist(page_service, actor, tool_name, arguments, payload):
    from datetime import UTC, datetime

    from backend.services.mcp_trace_capture import persist_mcp_tool_trace

    now = datetime.now(UTC)
    with page_service.session_factory.begin() as session:
        call = persist_mcp_tool_trace(
            session=session,
            actor=actor,
            tool_name=tool_name,
            arguments=arguments,
            payload=payload,
            mcp_session_id=f"conv-{uuid4()}",
            started_at=now,
            completed_at=now,
            latency_ms=1,
        )
        assert call is not None
        return call.id


def test_persist_trace_stores_the_masked_edit_receipt(page_service, mcp_default_actor):
    from backend.models.mcp_trace import MCPToolCall

    actor = mcp_default_actor(scopes=("read", "draft-write"))
    with page_service.session_factory() as session:
        workspace_id = session.scalar(
            select(Workspace).where(Workspace.slug == "default")
        ).id
    page_id = uuid4()
    version_id = uuid4()
    receipt = {
        "edit_mode": "patch",
        "editing_revision_before": 2,
        "editing_revision_after": 3,
        "chars_added": 9,
        "chars_removed": 4,
        "lines_added": 1,
        "lines_removed": 1,
        "changed_sections": [["Guide"]],
        "first_change": {
            "line": 3,
            "before_preview": "old access_token=CAPTURE-SECRET",
            "after_preview": "new line",
        },
        "operations": [
            {
                "index": 0,
                "op": "replace_text",
                "line_range": [3, 3],
                "heading_path": ["Guide"],
                "affected_block_ids": ["b_1"],
                "chars_added": 9,
                "chars_removed": 4,
            }
        ],
    }
    call_id = _persist(
        page_service,
        actor,
        "patch_context_edit",
        {
            "workspace_id": str(workspace_id),
            "page_id": str(page_id),
            "version_id": str(version_id),
            "expected_editing_revision": 2,
            "operations": [
                {"op": "replace_text", "old_text": "old", "new_text": "new line"}
            ],
        },
        _edit_trace_payload(
            workspace_id,
            data={
                "id": str(page_id),
                "version_id": str(version_id),
                "content_hash": "a" * 64,
                "body_md": "FULL-BODY-MUST-NOT-BE-STORED",
                "edit_receipt": receipt,
            },
        ),
    )

    with page_service.session_factory() as session:
        stored = session.get(MCPToolCall, call_id)
        detail = stored.result_detail
        input_summary = stored.input_summary

    assert detail["page_id"] == str(page_id)
    assert detail["version_id"] == str(version_id)
    assert detail["edit_receipt"]["chars_added"] == 9
    assert detail["edit_receipt"]["operations"][0]["affected_block_ids"] == ["b_1"]
    assert "CAPTURE-SECRET" not in json.dumps(detail)
    assert "FULL-BODY-MUST-NOT-BE-STORED" not in json.dumps(detail)
    assert input_summary["operations"][0]["new_text_preview"] == {
        "text": "new line",
        "truncated": False,
    }


def test_persist_trace_stores_no_detail_for_a_read_or_a_failed_edit(
    page_service, mcp_default_actor
):
    from backend.models.mcp_trace import MCPToolCall

    actor = mcp_default_actor(scopes=("read", "draft-write"))
    with page_service.session_factory() as session:
        workspace_id = session.scalar(
            select(Workspace).where(Workspace.slug == "default")
        ).id
    read_id = _persist(
        page_service,
        actor,
        "read",
        {"workspace_id": str(workspace_id), "target_type": "context_page"},
        _edit_trace_payload(workspace_id, data={"edit_receipt": {"forged": True}}),
    )
    failed_id = _persist(
        page_service,
        actor,
        "patch_context_edit",
        {"workspace_id": str(workspace_id), "page_id": str(uuid4())},
        _edit_trace_payload(workspace_id, data=None, result_status="failed"),
    )

    with page_service.session_factory() as session:
        assert session.get(MCPToolCall, read_id).result_detail is None
        assert session.get(MCPToolCall, failed_id).result_detail is None


def test_persist_trace_keeps_a_failed_edits_error_structure_not_its_prose(
    page_service, mcp_default_actor
):
    from backend.models.mcp_trace import MCPToolCall

    actor = mcp_default_actor(scopes=("read", "draft-write"))
    with page_service.session_factory() as session:
        workspace_id = session.scalar(
            select(Workspace).where(Workspace.slug == "default")
        ).id
    payload = _edit_trace_payload(workspace_id, data=None, result_status="failed")
    payload["envelope"]["error_code"] = "text_not_found"
    payload["envelope"]["error_details"] = [
        {
            "field": "operations[0].old_text",
            "code": "text_not_found",
            "message": "old_text 'CAPTURED-DETAIL-SECRET' was not found",
            "fix": "Re-read the section first.",
        }
    ]
    call_id = _persist(
        page_service,
        actor,
        "patch_context_edit",
        {"workspace_id": str(workspace_id), "page_id": str(uuid4())},
        payload,
    )

    with page_service.session_factory() as session:
        detail = session.get(MCPToolCall, call_id).result_detail

    assert detail == {
        "error_details": [
            {
                "code": "text_not_found",
                "field": "operations[0].old_text",
                "operation_index": 0,
                "conflict": None,
            }
        ]
    }

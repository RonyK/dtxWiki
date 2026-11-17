"""The merge tools share persisted drafts and the runner's recovery path."""

import json
from uuid import UUID, uuid4

import anyio
from sqlalchemy import func, select

from backend.mcp.protocol.runner import call_tool_async
from backend.mcp.router import dispatch
from backend.models.context import ContextPageVersion
from backend.models.mcp_chat import McpChatSession, McpChatToolCallArtifact
from backend.models.mcp_trace import MCPToolCall
from backend.models.workspace import Workspace


def test_merge_tools_publish_and_recover_the_prepared_result(
    page_service, mcp_default_actor
):
    actor = mcp_default_actor(actor_id="merge-tools", scopes=("mcp:write",))
    workspace_id = actor.workspace_id
    with page_service.session_factory.begin() as session:
        workspace = session.get(Workspace, workspace_id)
        policy = dict(workspace.review_policy)
        policy["review_required"] = {
            **policy["review_required"],
            "always_for_ai_authored": False,
        }
        workspace.review_policy = policy

    def create(slug):
        return page_service.write_page(
            workspace_slug="default",
            slug=slug,
            title="Merge tools",
            body_md=(
                "# Merge tools\n\nFirst paragraph.\n\n"
                "Middle paragraph.\n\nLast paragraph.\n"
            ),
            actor_kind="human",
            actor_id=str(actor.user_id),
            actor_provenance="browser_session",
        )

    initial, reference = create("merge-tools"), create("merge-reference")

    def changed(text):
        draft = page_service.begin_context_edit(
            initial.id,
            workspace_id,
            "human",
            str(actor.user_id),
            "browser_session",
            str(uuid4()),
            from_version_id=initial.version_id,
        )
        return page_service.autosave_page(
            page_id=initial.id,
            workspace_id=workspace_id,
            title=draft.title,
            body_md=text,
            actor_kind="human",
            actor_id=str(actor.user_id),
            actor_provenance="browser_session",
            version_id=draft.version_id,
            expected_editing_revision=draft.editing_revision,
            base_version_id=draft.version_id,
            base_content_hash=draft.content_hash,
            idempotency_key=str(uuid4()),
        )

    draft = changed(initial.body_md.replace("Last paragraph.", "Draft contribution."))
    cited = dispatch(
        "cite",
        {
            "workspace_id": str(workspace_id),
            "page_id": str(initial.id),
            "version_id": str(draft.version_id),
            "expected_editing_revision": draft.editing_revision,
            "idempotency_key": "merge-reference",
            "add": [{"target_kind": "context_page", "target_id": str(reference.id)}],
        },
        actor=actor,
        page_service=page_service,
    )
    assert cited.envelope.result_status == "succeeded", cited.envelope
    head = changed(
        initial.body_md.replace("First paragraph.", "Published contribution.")
    )
    seen = page_service.preview_context_merge(initial.id, head.version_id, workspace_id)
    page_service.publish_draft(
        page_id=initial.id,
        version_id=head.version_id,
        workspace_id=workspace_id,
        reason="Prepare the newer head",
        publish_despite_findings=False,
        actor_kind="human",
        actor_id=str(actor.user_id),
        actor_provenance="browser_session",
        expected_editing_revision=seen["editing_revision"],
        expected_published_version_id=seen["published_version_id"],
        result_hash=seen["result"]["result_hash"],
        idempotency_key="merge-head",
    )
    token = "new"
    common = {
        "workspace_id": str(workspace_id),
        "page_id": f"/p/{workspace_id}/{initial.id}",
        "version_id": str(draft.version_id),
    }

    def call(tool, expect_error=None, **arguments):
        nonlocal token

        async def run():
            return await call_tool_async(
                tool,
                {
                    **(
                        {"workspace_id": str(workspace_id)}
                        if tool == "get_operation_status"
                        else common
                    ),
                    "conversation_token": token,
                    **arguments,
                },
                actor=actor,
                page_service=page_service,
                session_factory=page_service.session_factory,
            )

        result = anyio.run(run)
        payload = result.structuredContent
        assert payload == json.loads(result.content[0].text)
        if expect_error is not None:
            assert payload["envelope"]["error_code"] == expect_error, payload
            return None
        assert not result.isError, payload
        token = payload["envelope"]["conversation_token"]
        return payload["data"]

    preview = call("preview_context_merge")
    assert preview["kind"] == "automatic_merge"
    # Both choices are sent only for a conflict.
    assert "keep_both" not in preview and "latest" not in preview
    # Naming a commit previews what bring-in would merge: the draft already
    # contains the version it started from.
    started_from = call(
        "preview_context_merge", commit_version_id=str(initial.version_id)
    )
    assert started_from["kind"] == "fast_forward"
    assert preview["page_id"] == str(initial.id)
    assert "Published contribution." in preview["result"]["body_md"]
    assert "Draft contribution." in preview["result"]["body_md"]
    with page_service.session_factory() as session:
        assert session.scalar(select(McpChatSession.write_count)) == 0
    bring_arguments = {
        "expected_editing_revision": preview["editing_revision"],
        "commit_version_id": preview["published_version_id"],
        "idempotency_key": "merge-bring",
    }
    brought = call("bring_in_context_version", **bring_arguments)
    replayed = call("bring_in_context_version", **bring_arguments)
    assert brought["editing_revision"] == preview["editing_revision"] + 1
    assert replayed["editing_revision"] == brought["editing_revision"]
    assert replayed["replayed"] is True
    # The draft now contains the published version: nothing to merge.
    again = call(
        "bring_in_context_version",
        **{
            **bring_arguments,
            "expected_editing_revision": brought["editing_revision"],
            "idempotency_key": "merge-again",
        },
    )
    assert again["editing_revision"] == brought["editing_revision"]
    # Another page's version is not in this page's history.
    call(
        "bring_in_context_version",
        expect_error="not_found",
        **{
            **bring_arguments,
            "expected_editing_revision": brought["editing_revision"],
            "commit_version_id": str(reference.version_id),
            "idempotency_key": "merge-foreign",
        },
    )
    final_body = preview["result"]["body_md"].replace(
        "Middle paragraph.", "Prepared final result."
    )
    metadata = {"tags": ["merge-result"]}
    final_preview = call(
        "preview_context_merge", body_md=final_body, page_metadata=metadata
    )
    publish_arguments = {
        "expected_editing_revision": final_preview["editing_revision"],
        "expected_published_version_id": final_preview["published_version_id"],
        "result_hash": final_preview["result"]["result_hash"],
        "result_body_md": final_body,
        "result_page_metadata": metadata,
        "reason": "Publish the reviewed result",
        "idempotency_key": "merge-publish",
    }
    # Omitting the published version means "none expected": with a head it
    # is stale, never a blind overwrite.
    call(
        "publish_context_edit",
        expect_error="stale_base_version",
        **{
            key: value
            for key, value in publish_arguments.items()
            if key != "expected_published_version_id"
        }
        | {"idempotency_key": "merge-publish-headless"},
    )
    published = call("publish_context_edit", **publish_arguments)
    recovered = call("get_operation_status", idempotency_key="merge-publish")
    assert recovered["status"] == "committed"
    assert recovered["result"]["version_id"] == published["version_id"]
    repeated = call("publish_context_edit", **publish_arguments)
    assert repeated["replayed"] is True
    assert repeated["version_id"] == published["version_id"]
    with page_service.session_factory() as session:
        stored = session.get(ContextPageVersion, UUID(published["version_id"]))
        assert stored.version_status == "published_snapshot"
        assert "Prepared final result." in stored.body_md
        assert stored.page_metadata["tags"] == ["merge-result"]
        assert stored.parent_version_id == initial.version_id
        from backend.services.document_references import read_document_references

        references = read_document_references(session, version=stored)
        assert [item.target_id for item in references] == [reference.id]
        assert (
            session.scalar(
                select(func.count())
                .select_from(ContextPageVersion)
                .where(ContextPageVersion.page_id == initial.id)
            )
            == 3
        )
        previews = session.scalars(
            select(MCPToolCall).where(MCPToolCall.tool_name == "preview_context_merge")
        ).all()
        assert all(row.result_detail is None for row in previews)
        preview_edges = session.scalars(
            select(McpChatToolCallArtifact).where(
                McpChatToolCallArtifact.mcp_tool_call_id.in_(
                    [row.id for row in previews]
                )
            )
        ).all()
        assert {edge.verb for edge in preview_edges} == {"referenced"}
        writes = session.scalars(
            select(MCPToolCall).where(
                MCPToolCall.tool_name.in_(
                    ["bring_in_context_version", "publish_context_edit"]
                ),
                MCPToolCall.result_status == "succeeded",
            )
        ).all()
        assert all(
            row.result_detail["version_id"] == published["version_id"] for row in writes
        )
        captured = json.dumps([row.input_summary for row in [*previews, *writes]])
        # Bodies are kept as digests only, as for the other edit tools.
        assert "Prepared final result." not in captured
        assert "result_body_md_digest" in captured and "body_md_digest" in captured

"""Patch receipts describe the final persisted body after save-time fixing."""

from sqlalchemy import select

from backend.models.context import ContextPage, ContextPageVersion
from backend.models.mcp_trace import MCPWriteIdempotency
from backend.models.workspace import Workspace
from backend.schemas.context_edit import ReplaceBlockOperation, ReplaceTextOperation
from backend.services.page_service import pending_page_metadata_patch
from backend.utils.content_hash import content_hash
from backend.utils.page_frontmatter import parse_page_frontmatter

ACTOR_ID = "00000000-0000-0000-0000-000000000010"


def test_context_edit_patch_fixes_final_body_and_replays_matching_receipt(
    page_service, session_factory
):
    with session_factory.begin() as session:
        workspace = session.scalar(select(Workspace).where(Workspace.slug == "default"))
        workspace.auto_fix_enabled = True
        workspace_id = workspace.id
    before_body = (
        "# Patch\n\n- original\n\n"
        "| a   | b   |\n| --- | --- |\n| 1   | 2   |\n\nKeep this paragraph.\n"
    )
    created = page_service.create_page_first(
        workspace_slug="default",
        title="Patch",
        body_md=before_body,
        actor_kind="human",
        actor_id=ACTOR_ID,
        actor_provenance="mcp_client",
    )
    with session_factory() as session:
        before_map = [
            dict(entry)
            for entry in session.get(ContextPageVersion, created.version_id).block_map
        ]
    target = next(entry for entry in before_map if entry["kind"] == "table")
    list_target = next(entry for entry in before_map if entry["kind"] == "list")
    unchanged = {
        entry["hash"]: entry["id"]
        for entry in before_map
        if entry["id"] not in {target["id"], list_target["id"]}
    }
    command = dict(
        page_id=created.id,
        version_id=created.version_id,
        workspace_id=workspace_id,
        operations=[
            ReplaceBlockOperation(
                op="replace_block",
                block=target["id"],
                expected=target["hash"],
                content="|a|b|\n|-|-|\n|1|22222222|",
            ),
            ReplaceBlockOperation(
                op="replace_block",
                block=list_target["id"],
                expected=list_target["hash"],
                content="* revised   ",
            ),
        ],
        actor_kind="human",
        actor_id=ACTOR_ID,
        actor_provenance="mcp_client",
        expected_editing_revision=created.editing_revision,
        idempotency_key="patch-final-normalized-receipt",
    )
    patched = page_service.patch_context_edit(**command)
    final_body = (
        "# Patch\n\n- revised\n\n"
        "| a   | b        |\n| --- | -------- |\n| 1   | 22222222 |\n\n"
        "Keep this paragraph.\n"
    )
    with session_factory() as session:
        stored = session.get(ContextPageVersion, patched.version_id)
        assert stored.body_md == final_body
        assert stored.content_hash == content_hash(final_body)
        assert patched.body_md == stored.body_md
        assert patched.content_hash == stored.content_hash
        final_map = [dict(entry) for entry in stored.block_map]
        final_revision = stored.editing_revision
    changed = next(entry for entry in final_map if entry["kind"] == "table")
    changed_list = next(entry for entry in final_map if entry["kind"] == "list")
    assert changed["id"] == target["id"]
    assert changed_list["id"] == list_target["id"]
    assert changed["hash"] == content_hash(
        "| a   | b        |\n| --- | -------- |\n| 1   | 22222222 |\n"
    )
    assert changed_list["hash"] == content_hash("- revised\n")
    assert {
        entry["hash"]: entry["id"] for entry in final_map if entry["hash"] in unchanged
    } == unchanged
    receipt = patched._edit_receipt
    assert receipt is not None
    assert receipt.editing_revision_after == final_revision
    # The regions quote the stored body, so the caller sees the fixer's
    # padding it never sent, and is told the server adjusted its request.
    assert (
        receipt.changes[0].context_before,
        receipt.changes[0].removed,
        receipt.changes[0].added,
    ) == ("- ", "original", "revised")
    assert "| -------- |" in receipt.changes[1].added
    assert receipt.body_adjusted is True
    assert receipt.server_fixes == ["format"]
    assert receipt.chars_added - receipt.chars_removed == len(final_body) - len(
        before_body
    )
    assert {(block.id, block.kind, block.hash) for block in receipt.changed_blocks} == {
        (changed["id"], "table", changed["hash"]),
        (changed_list["id"], "list", changed_list["hash"]),
    }
    assert receipt.operations[0].affected_block_ids == [target["id"]]

    replayed = page_service.patch_context_edit(**command)
    assert replayed.model_dump() == patched.model_dump()
    assert replayed._edit_receipt.model_dump() == receipt.model_dump()
    # A receipt stored before `changes` replaced `first_change` still replays;
    # its regions are unknown rather than empty.
    with session_factory.begin() as session:
        row = session.scalar(
            select(MCPWriteIdempotency).where(
                MCPWriteIdempotency.idempotency_key == command["idempotency_key"]
            )
        )
        legacy = {
            key: value
            for key, value in row.result_payload["_edit_receipt"].items()
            if key
            not in {
                "changes",
                "changes_omitted_count",
                "body_adjusted",
                "server_fixes",
                "changed_blocks_omitted_count",
            }
        }
        legacy["first_change"] = {"line": 3, "before_preview": "", "after_preview": ""}
        row.result_payload = {**row.result_payload, "_edit_receipt": legacy}
    legacy_replay = page_service.patch_context_edit(**command)._edit_receipt
    assert legacy_replay.changes is None
    assert legacy_replay.body_adjusted is None
    assert legacy_replay.changed_blocks == receipt.changed_blocks
    with session_factory() as session:
        stored = session.get(ContextPageVersion, patched.version_id)
        assert stored.body_md == final_body
        assert stored.editing_revision == final_revision
        assert stored.block_map == final_map

    # The next operation can keep using the ID named by the first receipt.
    next_patch = page_service.patch_context_edit(
        **{
            **command,
            "operations": [
                ReplaceBlockOperation(
                    op="replace_block",
                    block=target["id"],
                    expected=changed["hash"],
                    content="|a|b|\n|-|-|\n|1|33333333|",
                )
            ],
            "expected_editing_revision": final_revision,
            "idempotency_key": "patch-final-normalized-receipt-next",
        }
    )
    assert next_patch.editing_revision == final_revision + 1
    assert next_patch._edit_receipt is not None
    assert next_patch._edit_receipt.operations[0].affected_block_ids == [target["id"]]

    # Continue the same edit through managed and partial writes. This exposes
    # the independent failure where body bytes, staged metadata and page title
    # diverge even though both writers return successful receipts.
    save_command = dict(
        page_id=created.id,
        workspace_id=workspace_id,
        actor_kind="human",
        actor_id=ACTOR_ID,
        actor_provenance="mcp_client",
        base_version_id=None,
        base_content_hash=None,
        editing_version_id=created.version_id,
        title=None,
    )
    staged = page_service.autosave_page(
        **save_command,
        body_md=next_patch.body_md,
        tags=["staged"],
        aliases=["Staged alias"],
        expected_editing_revision=next_patch.editing_revision,
        idempotency_key="metadata-staged",
    )
    with session_factory.begin() as session:
        session.get(ContextPage, created.id).title = "External title"
    unchanged_frontmatter = page_service.autosave_page(
        **save_command,
        body_md=staged.body_md.replace("Keep this paragraph.", "Keep this content."),
        expected_editing_revision=staged.editing_revision,
        idempotency_key="metadata-retain-external-title",
    )
    assert unchanged_frontmatter.title == "External title"
    retained = page_service.autosave_page(
        **save_command,
        body_md=parse_page_frontmatter(unchanged_frontmatter.body_md).body,
        expected_editing_revision=unchanged_frontmatter.editing_revision,
        idempotency_key="metadata-retain-staged",
    )
    assert (retained.title, retained.tags, retained.aliases) == (
        "External title",
        ["staged"],
        ["Staged alias"],
    )
    metadata_patch = page_service.patch_context_edit(
        **{
            **command,
            "operations": [
                ReplaceTextOperation(
                    op="replace_text",
                    old_text='title: "External title"',
                    new_text='title: "Patched title"',
                ),
                ReplaceTextOperation(
                    op="replace_text", old_text='tags: ["staged"]', new_text="tags: []"
                ),
                ReplaceTextOperation(
                    op="replace_text",
                    old_text='aliases: ["Staged alias"]',
                    new_text="aliases: []",
                ),
            ],
            "expected_editing_revision": retained.editing_revision,
            "idempotency_key": "metadata-patch-managed-frontmatter",
        }
    )
    assert (metadata_patch.title, metadata_patch.tags, metadata_patch.aliases) == (
        "Patched title",
        [],
        [],
    )
    parsed = parse_page_frontmatter(metadata_patch.body_md)
    assert (parsed.title, parsed.tags, parsed.aliases) == ("Patched title", [], [])
    with session_factory() as session:
        page = session.get(ContextPage, created.id)
        pending = pending_page_metadata_patch(
            session, page=page, version_id=metadata_patch.version_id
        )
        assert pending["title"] == "Patched title"
        assert pending["tags"] == pending["aliases"] == []
        assert page.title == "External title"

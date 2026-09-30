import pytest
from sqlalchemy import select

import backend.repositories.context_page_repo as context_page_repo_module
import backend.services.context_editing as context_editing_module
import backend.services.context_write_service as context_write_service_module
import backend.utils.block_identity as block_identity_module
from backend.mcp.router import dispatch
from backend.models.context import ContextPageVersion
from backend.models.workspace import Workspace
from backend.schemas.context_edit import PatchDraftRequest
from backend.schemas.page import BODY_MD_MAX_LENGTH
from backend.services.context_write_service import ContextWriteService
from backend.services.read_errors import ReadValidationError
from backend.utils.block_map import compute_blocks, serialize_blocks

# P8.6: patch_draft is no longer a callable MCP tool -- these tests call the
# still-fully-live ContextWriteService.patch_draft engine directly. See
# test_patch_draft_push_semantics.py for the same pattern.


@pytest.fixture
def actor(mcp_default_actor):
    return mcp_default_actor(scopes=("read", "draft-write"))


def _workspace_id(page_service):
    with page_service.session_factory() as session:
        return session.scalar(select(Workspace).where(Workspace.slug == "default")).id


def _structure_map(workspace_id, page_id, actor, page_service, **overrides):
    return dispatch(
        "read_structure_map",
        {
            "workspace_id": str(workspace_id),
            "target": {"page_id": str(page_id)},
            **overrides,
        },
        actor=actor,
        page_service=page_service,
    )


def _patch_draft(page_service, actor, workspace_id, **payload):
    """Engine-level equivalent of ``dispatch("patch_draft", {...})``."""
    with page_service.session_factory.begin() as session:
        return ContextWriteService().patch_draft(
            session=session,
            workspace_id=workspace_id,
            request=PatchDraftRequest(**payload),
            actor_kind=actor.actor_kind,
            actor_id=actor.actor_id,
        )


def _all_structure_map_blocks(workspace_id, page_id, actor, page_service):
    """Every block of a document, followed across pages.

    read_structure_map caps `blocks` (MCP-02) -- a 5,000-block document arrives
    200 at a time -- so a consumer that wants the whole map has to follow
    `next_cursor`. Reading `blocks[4_100]` off a single response used to work
    and now raises IndexError. Returns (document, every block in order).
    """

    blocks = []
    cursor = None
    while True:
        overrides = {"cursor": cursor} if cursor is not None else {}
        page = _structure_map(workspace_id, page_id, actor, page_service, **overrides)
        assert page.envelope.result_status == "succeeded", page.envelope
        blocks.extend(page.blocks)
        cursor = page.next_cursor
        if cursor is None:
            assert len(blocks) == page.document.block_count
            return page.document, blocks


def test_replace_block_via_patch_draft_lands_and_updates_the_structure_map(
    page_service, actor
):
    workspace_id = _workspace_id(page_service)
    body = "# Title\n\nfirst\n\nsecond\n"
    page = page_service.write_page(
        workspace_slug="default",
        slug="patch-block-replace",
        title="Patch Block Replace",
        body_md=body,
        actor_kind="human",
        actor_id="00000000-0000-0000-0000-000000000010",
        actor_provenance="browser_session",
    )
    before_map = _structure_map(workspace_id, page.id, actor, page_service)
    first_paragraph = next(b for b in before_map.blocks if b.preview == "first")

    result = _patch_draft(
        page_service,
        actor,
        workspace_id,
        page_id=page.id,
        base=str(before_map.document.version_id),
        reason="block-addressed replace",
        operations=[
            {
                "op": "replace_block",
                "block": first_paragraph.id,
                "expected": first_paragraph.hash,
                "content": "first, edited via block address",
            }
        ],
    )

    assert result.block_map_delta is not None
    assert len(result.block_map_delta) == 1
    replacement = result.block_map_delta[0]
    assert replacement.change == "replaced"
    assert replacement.old.id == replacement.new.id == first_paragraph.id
    assert replacement.old.hash == first_paragraph.hash
    after_map = _structure_map(workspace_id, page.id, actor, page_service)
    after_paragraph = next(b for b in after_map.blocks if b.id == first_paragraph.id)
    assert after_paragraph.preview.startswith("first, edited")
    # The heading and the untouched second paragraph both kept their ids.
    before_heading = next(b for b in before_map.blocks if b.kind == "h1")
    after_heading = next(b for b in after_map.blocks if b.kind == "h1")
    assert before_heading.id == after_heading.id
    before_second = next(b for b in before_map.blocks if b.preview == "second")
    after_second = next(b for b in after_map.blocks if b.preview == "second")
    assert before_second.id == after_second.id


def test_replace_block_rejects_a_stale_hash_with_a_recoverable_error(
    page_service, actor
):
    workspace_id = _workspace_id(page_service)
    page = page_service.write_page(
        workspace_slug="default",
        slug="patch-block-stale",
        title="Patch Block Stale",
        body_md="# Title\n\nfirst\n",
        actor_kind="human",
        actor_id="00000000-0000-0000-0000-000000000010",
        actor_provenance="browser_session",
    )
    structure_map = _structure_map(workspace_id, page.id, actor, page_service)
    paragraph = next(b for b in structure_map.blocks if b.kind == "p")

    with pytest.raises(ReadValidationError) as exc_info:
        _patch_draft(
            page_service,
            actor,
            workspace_id,
            page_id=page.id,
            base=str(structure_map.document.version_id),
            reason="stale replace",
            operations=[
                {
                    "op": "replace_block",
                    "block": paragraph.id,
                    "expected": "0" * 64,
                    "content": "edited",
                }
            ],
        )

    assert exc_info.value.error_code == "block_hash_mismatch"


def test_patch_draft_dry_run_predicts_the_block_map_without_persisting(
    page_service, actor
):
    workspace_id = _workspace_id(page_service)
    page = page_service.write_page(
        workspace_slug="default",
        slug="patch-block-dry-run",
        title="Patch Block Dry Run",
        body_md="# Title\n\nfirst\n",
        actor_kind="human",
        actor_id="00000000-0000-0000-0000-000000000010",
        actor_provenance="browser_session",
    )
    structure_map = _structure_map(workspace_id, page.id, actor, page_service)
    paragraph = next(b for b in structure_map.blocks if b.kind == "p")

    result = _patch_draft(
        page_service,
        actor,
        workspace_id,
        page_id=page.id,
        base=str(structure_map.document.version_id),
        reason="dry run predicted map",
        dry_run=True,
        operations=[
            {
                "op": "replace_block",
                "block": paragraph.id,
                "expected": paragraph.hash,
                "content": "predicted",
            }
        ],
    )

    assert result.block_map_delta is not None
    assert len(result.block_map_delta) == 1
    assert result.block_map_delta[0].change == "replaced"
    predicted = result.predicted_block_map
    assert predicted is not None
    predicted_paragraph = next(entry for entry in predicted if entry["kind"] == "p")
    assert predicted_paragraph["id"] == paragraph.id
    assert predicted_paragraph["hash"] != paragraph.hash

    after_map = _structure_map(workspace_id, page.id, actor, page_service)
    unchanged_paragraph = next(b for b in after_map.blocks if b.kind == "p")
    assert unchanged_paragraph.hash == paragraph.hash


def test_insert_row_via_patch_draft_lands_a_new_table_row(page_service, actor):
    workspace_id = _workspace_id(page_service)
    body = "| a | b |\n|---|---|\n| 1 | 2 |\n"
    page = page_service.write_page(
        workspace_slug="default",
        slug="patch-block-insert-row",
        title="Patch Block Insert Row",
        body_md=body,
        actor_kind="human",
        actor_id="00000000-0000-0000-0000-000000000010",
        actor_provenance="browser_session",
    )
    structure_map = _structure_map(workspace_id, page.id, actor, page_service)
    table = next(b for b in structure_map.blocks if b.kind == "table")
    row = table.rows[0]

    _patch_draft(
        page_service,
        actor,
        workspace_id,
        page_id=page.id,
        base=str(structure_map.document.version_id),
        reason="insert row",
        operations=[
            {
                "op": "insert_row",
                "row": row.id,
                "expected": row.hash,
                "content": "| 3 | 4 |",
            }
        ],
    )

    after_map = _structure_map(workspace_id, page.id, actor, page_service)
    after_table = next(b for b in after_map.blocks if b.kind == "table")
    assert len(after_table.rows) == 2
    assert after_table.rows[0].id == row.id


def test_one_megabyte_twenty_op_commit_uses_only_bounded_map_parses(
    page_service,
    actor,
    monkeypatch,
):
    workspace_id = _workspace_id(page_service)
    blocks = [f"block-{index} " + ("x" * 180) for index in range(5_000)]
    body = "\n\n".join(blocks)
    body += "x" * (BODY_MD_MAX_LENGTH - len(body) - 5_000)
    assert len(body) == BODY_MD_MAX_LENGTH - 5_000
    page = page_service.write_page(
        workspace_slug="default",
        slug="patch-block-one-megabyte",
        title="Patch Block One Megabyte",
        body_md=body,
        actor_kind="human",
        actor_id="00000000-0000-0000-0000-000000000010",
        actor_provenance="browser_session",
    )
    document, all_blocks = _all_structure_map_blocks(
        workspace_id, page.id, actor, page_service
    )
    targets = [all_blocks[index] for index in range(100, 4_100, 200)]
    parser_inputs: list[int] = []
    map_analysis_inputs: list[int] = []
    real_compute_blocks = block_identity_module.compute_blocks
    real_edit_compute_blocks = context_editing_module.compute_blocks
    real_blocks_from_map = context_editing_module.blocks_from_persisted_map

    def counted_compute_blocks(source: str):
        parser_inputs.append(len(source))
        return real_compute_blocks(source)

    def unexpected_full_map(*_args, **_kwargs):
        raise AssertionError("commit path recomputed the complete block map")

    def reject_full_document_edit_parse(source: str):
        if len(source) >= 2_000:
            raise AssertionError("commit path parsed the complete edit document")
        return real_edit_compute_blocks(source)

    def counted_blocks_from_map(source: str, block_map: list[dict]):
        map_analysis_inputs.append(len(source))
        return real_blocks_from_map(source, block_map)

    def unexpected_second_map_analysis(*_args, **_kwargs):
        raise AssertionError("commit path repeated the batch block-map analysis")

    with monkeypatch.context() as patch:
        patch.setattr(block_identity_module, "compute_blocks", counted_compute_blocks)
        patch.setattr(
            context_editing_module,
            "compute_blocks",
            reject_full_document_edit_parse,
        )
        patch.setattr(
            context_editing_module,
            "blocks_from_persisted_map",
            counted_blocks_from_map,
        )
        patch.setattr(
            block_identity_module,
            "blocks_from_persisted_map",
            unexpected_second_map_analysis,
        )
        patch.setattr(
            context_write_service_module,
            "compute_block_map_with_ids",
            unexpected_full_map,
        )
        patch.setattr(
            context_page_repo_module,
            "compute_block_map_with_ids",
            unexpected_full_map,
        )
        result = _patch_draft(
            page_service,
            actor,
            workspace_id,
            page_id=page.id,
            base=str(document.version_id),
            reason="bounded one-megabyte batch",
            operations=[
                {
                    "op": "insert_after",
                    "block": target.id,
                    "expected": target.hash,
                    "content": f"inserted-{index}",
                }
                for index, target in enumerate(targets)
            ],
        )

    assert map_analysis_inputs == [len(body)]
    # The batch shares the one map produced during edit validation with the
    # repository write: one bounded parse per disjoint touched window, never
    # a second persistence pass over the same 20 operations.
    assert len(parser_inputs) == 20
    assert max(parser_inputs) < 2_000
    assert sum(parser_inputs) < 20_000
    # Wall-clock ceilings are intentionally excluded: the default suite runs
    # under xdist, where host contention is unrelated to this algorithm. The
    # count and input-size bounds above are the deterministic performance gate.

    with page_service.session_factory() as session:
        persisted = session.get(ContextPageVersion, result.draft_version_id)
        assert persisted is not None
        fresh = serialize_blocks(compute_blocks(persisted.body_md))
        stored_content = [
            {
                key: (
                    [{"hash": row["hash"]} for row in value] if key == "rows" else value
                )
                for key, value in entry.items()
                if key != "id"
            }
            for entry in persisted.block_map
        ]
        assert stored_content == fresh

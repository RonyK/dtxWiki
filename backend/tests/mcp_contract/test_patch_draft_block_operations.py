from uuid import uuid4

import pytest
from sqlalchemy import select

import backend.repositories.context_page_repo as context_page_repo_module
import backend.services.context_editing as context_editing_module
import backend.services.page_service as page_service_module
import backend.utils.block_identity as block_identity_module
from backend.mcp.router import dispatch
from backend.models.context import ContextPageVersion
from backend.models.workspace import Workspace
from backend.schemas.page import BODY_MD_MAX_LENGTH
from backend.utils.block_map import compute_blocks, serialize_blocks

# Block-addressed operations through patch_context_edit on a shared draft:
# identity, hash validation and bounded-parser invariants.


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
            "page_id": str(page_id),
            **overrides,
        },
        actor=actor,
        page_service=page_service,
    )


def _begin(page_service, actor, workspace_id, page_id):
    """Start a shared draft from the published version."""
    begun = dispatch(
        "begin_context_edit",
        {
            "workspace_id": str(workspace_id),
            "page_id": str(page_id),
            "idempotency_key": str(uuid4()),
        },
        actor=actor,
        page_service=page_service,
    )
    assert begun.envelope.result_status == "succeeded", begun.envelope
    return begun.data


def _patch(page_service, actor, workspace_id, draft, operations):
    return dispatch(
        "patch_context_edit",
        {
            "workspace_id": str(workspace_id),
            "page_id": str(draft.page_id),
            "version_id": str(draft.version_id),
            "expected_editing_revision": draft.editing_revision,
            "operations": operations,
            "idempotency_key": str(uuid4()),
        },
        actor=actor,
        page_service=page_service,
    )


def test_replace_block_lands_and_updates_the_structure_map(page_service, actor):
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

    draft = _begin(page_service, actor, workspace_id, page.id)
    result = _patch(
        page_service,
        actor,
        workspace_id,
        draft,
        [
            {
                "op": "replace_block",
                "block": first_paragraph.id,
                "expected": first_paragraph.hash,
                "content": "first, edited via block address",
            }
        ],
    )

    assert result.envelope.result_status == "succeeded", result.envelope
    receipt = result.data.edit_receipt
    assert receipt.operations[0].affected_block_ids == [first_paragraph.id]
    assert [block.id for block in receipt.changed_blocks] == [first_paragraph.id]
    assert receipt.changed_blocks[0].hash != first_paragraph.hash
    after_map = _structure_map(
        workspace_id,
        page.id,
        actor,
        page_service,
        version_id=str(draft.version_id),
    )
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

    draft = _begin(page_service, actor, workspace_id, page.id)
    refused = _patch(
        page_service,
        actor,
        workspace_id,
        draft,
        [
            {
                "op": "replace_block",
                "block": paragraph.id,
                "expected": "0" * 64,
                "content": "edited",
            }
        ],
    )

    assert refused.envelope.error_code == "block_hash_mismatch", refused.envelope


def test_insert_row_lands_a_new_table_row(page_service, actor):
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

    draft = _begin(page_service, actor, workspace_id, page.id)
    result = _patch(
        page_service,
        actor,
        workspace_id,
        draft,
        [
            {
                "op": "insert_row",
                "row": row.id,
                "expected": row.hash,
                "content": "| 3 | 4 |",
            }
        ],
    )

    assert result.envelope.result_status == "succeeded", result.envelope
    after_map = _structure_map(
        workspace_id,
        page.id,
        actor,
        page_service,
        version_id=str(draft.version_id),
    )
    after_table = next(b for b in after_map.blocks if b.kind == "table")
    assert len(after_table.rows) == 2
    assert after_table.rows[0].id == row.id


# Capacity guard on a ~1 MB body; Release checks run it. The PR lane keeps the
# bounded-window parse on the same 20-op batch
# (test_block_batch_uses_gap_hash_fast_path_and_parses_only_bounded_windows)
# and the persisted block-map commits above.
@pytest.mark.release_scale
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
    # Take the base version and its block map from the same persisted row:
    # paging read_structure_map re-parses the whole body per page, and the
    # ids/hashes it returns are exactly this stored map.
    draft = _begin(page_service, actor, workspace_id, page.id)
    with page_service.session_factory() as session:
        entries = list(session.get(ContextPageVersion, draft.version_id).block_map)
    assert len(entries) == 5_000
    targets = [entries[index] for index in range(100, 4_100, 200)]
    assert all(entry["kind"] != "table" for entry in targets)
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
            page_service_module,
            "compute_block_map_with_ids",
            unexpected_full_map,
        )
        patch.setattr(
            context_page_repo_module,
            "compute_block_map_with_ids",
            unexpected_full_map,
        )
        result = _patch(
            page_service,
            actor,
            workspace_id,
            draft,
            [
                {
                    "op": "insert_after",
                    "block": target["id"],
                    "expected": target["hash"],
                    "content": f"inserted-{index}",
                }
                for index, target in enumerate(targets)
            ],
        )
    assert result.envelope.result_status == "succeeded", result.envelope

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
        persisted = session.get(ContextPageVersion, draft.version_id)
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

from time import perf_counter

import pytest

import backend.services.context_editing as context_editing_module
import backend.services.context_write_service as context_write_service_module
import backend.utils.block_identity as block_identity_module
from backend.schemas.context_edit import (
    DeleteBlockOperation,
    InsertAfterOperation,
    InsertRowOperation,
    ReplaceBlockOperation,
    ReplaceTextOperation,
)
from backend.schemas.page import BODY_MD_MAX_LENGTH
from backend.services.context_editing import (
    ContextEditError,
    EditResolutionContext,
    apply_operations,
    apply_operations_with_block_map,
    block_map_edit_plan,
    block_operation_provenance,
)
from backend.services.context_write_service import ContextWriteService
from backend.utils.block_identity import (
    BlockOperationProvenance,
    compute_block_map_with_ids,
    try_compute_incremental_block_map_with_ids,
)
from backend.utils.block_map import compute_blocks
from backend.utils.markdown_outline import parse_outline

pytestmark = pytest.mark.unit


def _seed(body: str) -> list[dict]:
    return compute_block_map_with_ids(
        body, previous_body_md=None, previous_block_map=None
    )


def _block(block_map: list[dict], kind: str, index: int = 0) -> dict:
    matches = [entry for entry in block_map if entry["kind"] == kind]
    return matches[index]


@pytest.mark.parametrize(
    "body",
    [
        "\n\n# Heading\n\nparagraph\n",
        "---\ntitle: Demo\n---\n\n# Heading\n\nparagraph\n",
        "# Heading\r\rparagraph\r\r- one\r- two",
        "> quote\n> continued\n\n```python\nprint('x')\n```\n",
        "| a | b |\n|---|---|\n| 1 | 2 |\n| 3 | 4 |\n",
        "<div>\nraw html\n</div>\n\n---\n\nafter\n",
        "- first\n\n  continuation\n\n- second\n\nlast\n",
    ],
)
def test_gap_hash_fast_path_reconstructs_parser_coordinates_without_parsing(body):
    block_map = _seed(body)
    context = EditResolutionContext(body=body, block_map=block_map)

    fast = context._fast_blocks_from_map()

    assert fast == compute_blocks(body)
    assert context._blocks_cache is None


def test_write_service_does_not_build_outline_for_block_only_batch(monkeypatch):
    body = "# Title\n\nfirst\n\nsecond\n"
    block_map = _seed(body)
    target = _block(block_map, "p", index=0)
    operation = ReplaceBlockOperation(
        op="replace_block",
        block=target["id"],
        expected=target["hash"],
        content="edited",
    )

    def unexpected_outline(_body: str):
        raise AssertionError("block-only writes must not parse a base outline")

    monkeypatch.setattr(
        context_write_service_module,
        "parse_outline",
        unexpected_outline,
    )

    result, edits, predicted_block_map = (
        ContextWriteService._apply_operations_with_restorations(
            body=body,
            block_map=block_map,
            operations=[operation],
            restorations=[],
        )
    )

    assert result == "# Title\n\nedited\n\nsecond\n"
    assert len(edits) == 1
    assert predicted_block_map is not None


def test_replace_block_swaps_content_and_keeps_the_rest_untouched():
    body = "# Title\n\nfirst\n\nsecond\n"
    block_map = _seed(body)
    target = _block(block_map, "p", index=0)

    op = ReplaceBlockOperation(
        op="replace_block",
        block=target["id"],
        expected=target["hash"],
        content="edited",
    )
    new_body, edits = apply_operations(
        body, parse_outline(body), [op], block_map=block_map
    )

    assert new_body == "# Title\n\nedited\n\nsecond\n"
    assert edits[0].no_op is False
    assert block_operation_provenance(edits) == (
        BlockOperationProvenance("replace_block", target["id"]),
    )


def test_replace_block_is_a_no_op_when_content_is_already_identical():
    body = "# Title\n\nfirst\n"
    block_map = _seed(body)
    target = _block(block_map, "p")

    op = ReplaceBlockOperation(
        op="replace_block", block=target["id"], expected=target["hash"], content="first"
    )
    new_body, edits = apply_operations(
        body, parse_outline(body), [op], block_map=block_map
    )

    assert new_body == body
    assert edits[0].no_op is True


def test_replace_block_rejects_a_stale_expected_hash():
    body = "# Title\n\nfirst\n"
    block_map = _seed(body)
    target = _block(block_map, "p")

    op = ReplaceBlockOperation(
        op="replace_block", block=target["id"], expected="0" * 64, content="edited"
    )
    with pytest.raises(ContextEditError) as excinfo:
        apply_operations(body, parse_outline(body), [op], block_map=block_map)
    assert excinfo.value.error_code == "block_hash_mismatch"


def test_replace_block_rejects_an_unknown_block_id():
    body = "# Title\n\nfirst\n"
    block_map = _seed(body)

    op = ReplaceBlockOperation(
        op="replace_block", block="b_doesnotexist", expected="0" * 64, content="edited"
    )
    with pytest.raises(ContextEditError) as excinfo:
        apply_operations(body, parse_outline(body), [op], block_map=block_map)
    assert excinfo.value.error_code == "block_not_found"


def test_insert_after_adds_a_new_top_level_block():
    body = "# Title\n\nfirst\n\nsecond\n"
    block_map = _seed(body)
    target = _block(block_map, "p", index=0)  # "first"

    op = InsertAfterOperation(
        op="insert_after",
        block=target["id"],
        expected=target["hash"],
        content="inserted",
    )
    new_body, edits = apply_operations(
        body, parse_outline(body), [op], block_map=block_map
    )

    assert new_body == "# Title\n\nfirst\n\ninserted\n\nsecond\n"
    assert block_operation_provenance(edits) == (
        BlockOperationProvenance("insert_after", target["id"], 1),
    )


def test_insert_after_the_last_block_appends_at_document_end():
    body = "# Title\n\nfirst\n"
    block_map = _seed(body)
    target = _block(block_map, "p")

    op = InsertAfterOperation(
        op="insert_after", block=target["id"], expected=target["hash"], content="second"
    )
    new_body, edits = apply_operations(
        body, parse_outline(body), [op], block_map=block_map
    )

    assert new_body == "# Title\n\nfirst\n\nsecond\n"


def test_delete_block_removes_exactly_the_target():
    body = "# Title\n\nfirst\n\nsecond\n"
    block_map = _seed(body)
    target = _block(block_map, "p", index=0)  # "first"

    op = DeleteBlockOperation(
        op="delete_block", block=target["id"], expected=target["hash"]
    )
    new_body, edits = apply_operations(
        body, parse_outline(body), [op], block_map=block_map
    )

    assert "first" not in new_body
    assert "second" in new_body
    assert "# Title" in new_body
    assert block_operation_provenance(edits) == (
        BlockOperationProvenance("delete_block", target["id"]),
    )


def test_insert_row_adds_a_new_row_after_the_target():
    body = "| a | b |\n|---|---|\n| 1 | 2 |\n| 3 | 4 |\n"
    block_map = _seed(body)
    table = _block(block_map, "table")
    first_row = table["rows"][0]

    op = InsertRowOperation(
        op="insert_row",
        row=first_row["id"],
        expected=first_row["hash"],
        content="| 9 | 9 |",
    )
    new_body, edits = apply_operations(
        body, parse_outline(body), [op], block_map=block_map
    )

    assert new_body == "| a | b |\n|---|---|\n| 1 | 2 |\n| 9 | 9 |\n| 3 | 4 |\n"
    assert block_operation_provenance(edits) == (
        BlockOperationProvenance("insert_row", first_row["id"], 1),
    )


def test_insert_row_after_the_last_row_extends_the_table():
    body = "| a | b |\n|---|---|\n| 1 | 2 |\n"
    block_map = _seed(body)
    table = _block(block_map, "table")
    only_row = table["rows"][0]

    op = InsertRowOperation(
        op="insert_row",
        row=only_row["id"],
        expected=only_row["hash"],
        content="| 3 | 4 |",
    )
    new_body, _ = apply_operations(body, parse_outline(body), [op], block_map=block_map)

    assert new_body == "| a | b |\n|---|---|\n| 1 | 2 |\n| 3 | 4 |\n"


def test_insert_row_rejects_an_unknown_row_id():
    body = "| a | b |\n|---|---|\n| 1 | 2 |\n"
    block_map = _seed(body)

    op = InsertRowOperation(
        op="insert_row", row="r_doesnotexist", expected="0" * 64, content="| 3 | 4 |"
    )
    with pytest.raises(ContextEditError) as excinfo:
        apply_operations(body, parse_outline(body), [op], block_map=block_map)
    assert excinfo.value.error_code == "row_not_found"


def test_a_block_op_without_a_block_map_reports_not_found_rather_than_crashing():
    body = "# Title\n\nfirst\n"
    op = ReplaceBlockOperation(
        op="replace_block", block="b_anything", expected="0" * 64, content="edited"
    )
    with pytest.raises(ContextEditError) as excinfo:
        apply_operations(body, parse_outline(body), [op], block_map=None)
    assert excinfo.value.error_code == "block_not_found"


def test_replace_block_rejects_content_that_expands_to_multiple_blocks():
    body = "# Title\n\nfirst\n"
    block_map = _seed(body)
    target = _block(block_map, "p")
    op = ReplaceBlockOperation(
        op="replace_block",
        block=target["id"],
        expected=target["hash"],
        content="one\n\ntwo",
    )

    with pytest.raises(ContextEditError) as excinfo:
        apply_operations(body, parse_outline(body), [op], block_map=block_map)

    assert excinfo.value.error_code == "operation_unsupported"


def test_insert_row_rejects_content_that_expands_to_multiple_rows():
    body = "| a | b |\n|---|---|\n| 1 | 2 |\n"
    block_map = _seed(body)
    target = _block(block_map, "table")["rows"][0]
    op = InsertRowOperation(
        op="insert_row",
        row=target["id"],
        expected=target["hash"],
        content="| 3 | 4 |\n| 5 | 6 |",
    )

    with pytest.raises(ContextEditError) as excinfo:
        apply_operations(body, parse_outline(body), [op], block_map=block_map)

    assert excinfo.value.error_code == "operation_unsupported"


def test_block_operation_preserves_bare_cr_coordinates_and_newline_style():
    body = "# Title\r\rfirst\r\rsecond"
    block_map = _seed(body)
    target = _block(block_map, "p", index=0)
    op = ReplaceBlockOperation(
        op="replace_block",
        block=target["id"],
        expected=target["hash"],
        content="edited",
    )

    new_body, _ = apply_operations(body, parse_outline(body), [op], block_map=block_map)

    assert new_body == "# Title\r\redited\r\rsecond"


def test_block_batch_uses_gap_hash_fast_path_and_parses_only_bounded_windows(
    monkeypatch,
):
    body = "\n\n".join(f"block-{index} " + ("x" * 170) for index in range(5_000))
    block_map = _seed(body)
    operations = [
        InsertAfterOperation(
            op="insert_after",
            block=block_map[index]["id"],
            expected=block_map[index]["hash"],
            content=f"inserted-{index}",
        )
        for index in range(100, 4_100, 200)
    ]
    calls: list[int] = []
    real_compute_blocks = block_identity_module.compute_blocks

    def counted_compute_blocks(source: str):
        calls.append(len(source))
        return real_compute_blocks(source)

    monkeypatch.setattr(
        block_identity_module,
        "compute_blocks",
        counted_compute_blocks,
    )

    result, edits = apply_operations(
        body,
        None,
        operations,
        block_map=block_map,
    )

    assert len(edits) == 20
    assert len(calls) == 20
    assert max(calls) < 1_000
    assert sum(calls) < 20_000


def test_one_megabyte_twenty_op_prefix_map_benchmark_beats_full_reparse_by_work(
    monkeypatch,
    record_property,
):
    """Pin §13.6's two paths to deterministic parser-work measurements."""

    blocks = [f"block-{index} " + ("x" * 180) for index in range(5_000)]
    body = "\n\n".join(blocks)
    body += "x" * (BODY_MD_MAX_LENGTH - len(body) - 5_000)
    assert len(body) == BODY_MD_MAX_LENGTH - 5_000
    block_map = _seed(body)
    operations = [
        InsertAfterOperation(
            op="insert_after",
            block=block_map[index]["id"],
            expected=block_map[index]["hash"],
            content=f"inserted-{index}",
        )
        for index in range(100, 4_100, 200)
    ]

    prefix_parser_inputs: list[int] = []
    prefix_map_inputs: list[int] = []
    authoritative_parser_inputs: list[int] = []
    real_compute_blocks = block_identity_module.compute_blocks
    real_blocks_from_map = context_editing_module.blocks_from_persisted_map

    def count_prefix_parse(source: str):
        prefix_parser_inputs.append(len(source))
        return real_compute_blocks(source)

    def count_map_analysis(source: str, stored_map: list[dict]):
        prefix_map_inputs.append(len(source))
        return real_blocks_from_map(source, stored_map)

    def reject_repeated_map_analysis(*_args, **_kwargs):
        raise AssertionError("prefix path repeated the persisted-map analysis")

    monkeypatch.setattr(block_identity_module, "compute_blocks", count_prefix_parse)
    monkeypatch.setattr(
        context_editing_module,
        "blocks_from_persisted_map",
        count_map_analysis,
    )
    monkeypatch.setattr(
        block_identity_module,
        "blocks_from_persisted_map",
        reject_repeated_map_analysis,
    )
    prefix_started = perf_counter()
    applied = apply_operations_with_block_map(
        body,
        None,
        operations,
        block_map=block_map,
        identity_seed="comparative-benchmark",
    )
    prefix_elapsed = perf_counter() - prefix_started
    assert applied.predicted_block_map is not None

    def count_authoritative_parse(source: str):
        authoritative_parser_inputs.append(len(source))
        return real_compute_blocks(source)

    monkeypatch.setattr(
        block_identity_module,
        "compute_blocks",
        count_authoritative_parse,
    )
    authoritative_started = perf_counter()
    authoritative = compute_block_map_with_ids(
        applied.body,
        previous_body_md=body,
        previous_block_map=block_map,
        operation_provenance=block_operation_provenance(applied.edits),
        identity_seed="comparative-benchmark",
    )
    authoritative_elapsed = perf_counter() - authoritative_started

    assert applied.predicted_block_map.value == authoritative
    assert prefix_map_inputs == [len(body)]
    assert len(prefix_parser_inputs) == 20
    assert max(prefix_parser_inputs) < 2_000
    assert sum(prefix_parser_inputs) < 20_000
    assert authoritative_parser_inputs == [len(applied.body), len(body)]
    assert sum(authoritative_parser_inputs) > 1_900_000
    assert prefix_elapsed < 3.0
    assert authoritative_elapsed < 10.0

    record_property("fixture_chars", len(body))
    record_property("operation_count", len(operations))
    record_property("prefix_map_analysis_calls", len(prefix_map_inputs))
    record_property("prefix_local_parse_calls", len(prefix_parser_inputs))
    record_property("prefix_local_parse_chars", sum(prefix_parser_inputs))
    record_property(
        "authoritative_full_parse_calls",
        len(authoritative_parser_inputs),
    )
    record_property(
        "authoritative_full_parse_chars",
        sum(authoritative_parser_inputs),
    )
    record_property("prefix_elapsed_ms", round(prefix_elapsed * 1_000, 3))
    record_property(
        "authoritative_elapsed_ms",
        round(authoritative_elapsed * 1_000, 3),
    )


def test_incremental_maps_match_the_authoritative_full_parse():
    cases = [
        (
            "# Root\n\n## Child\n\nfirst\n\nsecond\n",
            lambda block_map: ReplaceBlockOperation(
                op="replace_block",
                block=_block(block_map, "p", index=0)["id"],
                expected=_block(block_map, "p", index=0)["hash"],
                content="edited paragraph",
            ),
        ),
        (
            "# Root\n\nfirst\n\nsecond\n",
            lambda block_map: InsertAfterOperation(
                op="insert_after",
                block=_block(block_map, "p", index=0)["id"],
                expected=_block(block_map, "p", index=0)["hash"],
                content="inserted paragraph",
            ),
        ),
        (
            "# Root\n\nfirst\n\nmiddle\n\nlast\n",
            lambda block_map: DeleteBlockOperation(
                op="delete_block",
                block=_block(block_map, "p", index=1)["id"],
                expected=_block(block_map, "p", index=1)["hash"],
            ),
        ),
        (
            "# Root\n\n| a | b |\n|---|---|\n| 1 | 2 |\n| 3 | 4 |\n\nend\n",
            lambda block_map: InsertRowOperation(
                op="insert_row",
                row=_block(block_map, "table")["rows"][0]["id"],
                expected=_block(block_map, "table")["rows"][0]["hash"],
                content="| 9 | 9 |",
            ),
        ),
    ]
    for body, make_operation in cases:
        before_map = _seed(body)
        new_body, edits = apply_operations(
            body,
            None,
            [make_operation(before_map)],
            block_map=before_map,
        )
        plan = block_map_edit_plan(edits)
        assert plan is not None
        incremental = try_compute_incremental_block_map_with_ids(
            new_body,
            previous_body_md=body,
            previous_block_map=before_map,
            edits=plan,
            identity_seed="equivalence",
        )
        authoritative = compute_block_map_with_ids(
            new_body,
            previous_body_md=body,
            previous_block_map=before_map,
            operation_provenance=block_operation_provenance(edits),
            identity_seed="equivalence",
        )
        assert incremental == authoritative


def test_replace_block_is_isolated_from_a_directly_adjacent_neighbor():
    """A heading can be followed on the very next line by a paragraph. A
    paragraph written in its place would lazily absorb that neighbour, so the
    replacement is padded with the blank line that keeps both blocks."""

    body = "# Heading\nparagraph\n"
    block_map = _seed(body)
    heading = _block(block_map, "h1")
    operation = ReplaceBlockOperation(
        op="replace_block",
        block=heading["id"],
        expected=heading["hash"],
        content="replacement paragraph",
    )

    result, _edits = apply_operations(body, None, [operation], block_map=block_map)

    assert result == "replacement paragraph\n\nparagraph\n"
    assert [block.kind for block in compute_blocks(result)] == ["p", "p"]


def test_replace_block_that_reparses_a_neighbor_is_a_reflow_error():
    """Padding cannot help when the new block claims what follows it: a list
    item takes the indented block after it as a continuation."""

    body = "Para\n\n    indented code\n"
    block_map = _seed(body)
    paragraph = _block(block_map, "p")
    operation = ReplaceBlockOperation(
        op="replace_block",
        block=paragraph["id"],
        expected=paragraph["hash"],
        content="- item",
    )

    with pytest.raises(ContextEditError) as excinfo:
        apply_operations(body, None, [operation], block_map=block_map)

    assert excinfo.value.error_code == "block_reflow"
    assert excinfo.value.details


def test_block_batch_falls_back_for_a_legacy_map_and_self_heals_cache_fields(
    monkeypatch,
):
    body = "# Title\n\nfirst\n\nsecond\n"
    current_map = _seed(body)
    legacy_map = [
        {key: value for key, value in entry.items() if key != "gap"}
        for entry in current_map
    ]
    target = _block(legacy_map, "p", index=0)
    operation = ReplaceBlockOperation(
        op="replace_block",
        block=target["id"],
        expected=target["hash"],
        content="edited",
    )
    calls: list[int] = []
    real_compute_blocks = context_editing_module.compute_blocks

    def counted_compute_blocks(source: str):
        calls.append(len(source))
        return real_compute_blocks(source)

    monkeypatch.setattr(
        context_editing_module,
        "compute_blocks",
        counted_compute_blocks,
    )

    result, edits = apply_operations(
        body,
        parse_outline(body),
        [operation],
        block_map=legacy_map,
    )
    healed_map = compute_block_map_with_ids(
        result,
        previous_body_md=body,
        previous_block_map=legacy_map,
        operation_provenance=block_operation_provenance(edits),
    )

    # The missing cache edge forces one authoritative base parse. The proven
    # base coordinates are then reused for the bounded final splice, which
    # also self-heals ``gap`` without another full-document parse.
    assert calls == [len(body)]
    assert all("gap" in entry for entry in healed_map)
    assert _block(healed_map, "p", index=0)["id"] == target["id"]


def test_collective_validator_detects_raw_changes_outside_the_target(monkeypatch):
    body = "# Title\n\nfirst\n\nsecond\n"
    block_map = _seed(body)
    target = _block(block_map, "p", index=0)
    operation = ReplaceBlockOperation(
        op="replace_block",
        block=target["id"],
        expected=target["hash"],
        content="edited",
    )
    real_apply = context_editing_module.apply_resolved_edits

    def corrupt_untouched_block(source, edits):
        result = real_apply(source, edits)
        return result.replace("second\n", "second  \n")

    monkeypatch.setattr(
        context_editing_module,
        "apply_resolved_edits",
        corrupt_untouched_block,
    )

    with pytest.raises(ContextEditError) as excinfo:
        apply_operations(
            body,
            parse_outline(body),
            [operation],
            block_map=block_map,
        )

    assert excinfo.value.error_code == "operation_unsupported"


def test_block_operations_reject_mixed_address_types_and_overlapping_targets():
    body = "# Title\n\nfirst\n"
    block_map = _seed(body)
    target = _block(block_map, "p")
    replace_block = ReplaceBlockOperation(
        op="replace_block",
        block=target["id"],
        expected=target["hash"],
        content="edited",
    )

    with pytest.raises(ContextEditError) as mixed:
        apply_operations(
            body,
            parse_outline(body),
            [
                replace_block,
                ReplaceTextOperation(
                    op="replace_text",
                    old_text="Title",
                    new_text="Heading",
                ),
            ],
            block_map=block_map,
        )
    assert mixed.value.error_code == "operation_unsupported"

    with pytest.raises(ContextEditError) as overlapping:
        apply_operations(
            body,
            parse_outline(body),
            [replace_block, replace_block],
            block_map=block_map,
        )
    assert overlapping.value.error_code == "operations_overlap"


def test_two_touching_replaced_blocks_share_one_separator():
    """Each replace_block pads itself against its original neighbours, so a
    batch that replaces two blocks which touch must not leave two blank lines
    at their seam."""

    body = "# Title\nText right under the heading.\n\nTail paragraph."
    block_map = _seed(body)
    first, second = block_map[0], block_map[1]
    operations = [
        ReplaceBlockOperation(
            op="replace_block",
            block=first["id"],
            expected=first["hash"],
            content="Plain paragraph now.",
        ),
        ReplaceBlockOperation(
            op="replace_block",
            block=second["id"],
            expected=second["hash"],
            content="Second paragraph now.",
        ),
    ]

    result, _edits = apply_operations(body, None, operations, block_map=block_map)

    assert result == (
        "Plain paragraph now.\n\nSecond paragraph now.\n\nTail paragraph."
    )

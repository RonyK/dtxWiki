from datetime import UTC, datetime
from uuid import uuid4

import pytest

from backend.schemas.context_edit import (
    DeleteBlockOperation,
    InsertAfterOperation,
    InsertRowOperation,
    ReplaceBlockOperation,
    ReplaceTextOperation,
)
from backend.services.context_push import (
    actual_target_ids_for_operations,
    build_clean_changes,
    build_conflict_details,
    decide_push_outcome,
    hash_map,
    reconciliation_target_sets,
    resolve_target_id_between_maps,
    target_ids_for_operations,
)
from backend.utils.block_identity import compute_block_map_with_ids

pytestmark = pytest.mark.unit

_BASE_ID = uuid4()
_HEAD_ID = uuid4()


def _map(*, b1="h1", b2="h1") -> list[dict]:
    return [
        {"id": "b_1", "kind": "p", "hash": b1, "lines": 1},
        {"id": "b_2", "kind": "p", "hash": b2, "lines": 1},
    ]


def test_same_base_and_head_is_always_committed_even_with_no_maps():
    decision = decide_push_outcome(
        base_version_id=_HEAD_ID,
        head_version_id=_HEAD_ID,
        base_block_map=[],
        head_block_map=[],
        target_ids=frozenset({"b_1"}),
    )
    assert decision.outcome == "committed"
    assert decision.changed_ids == frozenset()


def test_different_base_and_head_with_untouched_target_is_rebased():
    decision = decide_push_outcome(
        base_version_id=_BASE_ID,
        head_version_id=_HEAD_ID,
        base_block_map=_map(b1="h1", b2="h1"),
        head_block_map=_map(b1="h1", b2="CHANGED"),
        target_ids=frozenset({"b_1"}),
    )
    assert decision.outcome == "rebased"
    assert decision.changed_ids == frozenset({"b_2"})


def test_different_base_and_head_with_touched_target_is_diverged():
    decision = decide_push_outcome(
        base_version_id=_BASE_ID,
        head_version_id=_HEAD_ID,
        base_block_map=_map(b1="h1", b2="h1"),
        head_block_map=_map(b1="CHANGED", b2="h1"),
        target_ids=frozenset({"b_1"}),
    )
    assert decision.outcome == "diverged"
    assert decision.changed_ids == frozenset({"b_1"})

    base_body = "caf\u00e9\ncontinued\n\nbeta\n"
    head_body = "caf\u00e9  \ncontinued\n\nbeta\n"
    base_map = compute_block_map_with_ids(
        base_body, previous_body_md=None, previous_block_map=None
    )
    head_map = compute_block_map_with_ids(
        head_body, previous_body_md=base_body, previous_block_map=base_map
    )
    target = base_map[0]
    assert head_map[0]["hash"] == target["hash"]
    hard_break = decide_push_outcome(
        base_version_id=_BASE_ID,
        head_version_id=_HEAD_ID,
        base_block_map=base_map,
        head_block_map=head_map,
        target_ids=frozenset({target["id"]}),
        base_body_md=base_body,
        head_body_md=head_body,
    )
    assert hard_break.outcome == "diverged"
    assert hard_break.changed_ids == frozenset({target["id"]})

    equivalent_body = "cafe\u0301\r\ncontinued\r\n\r\nbeta\r\n"
    equivalent_map = compute_block_map_with_ids(
        equivalent_body, previous_body_md=base_body, previous_block_map=base_map
    )
    equivalent = decide_push_outcome(
        base_version_id=_BASE_ID,
        head_version_id=_HEAD_ID,
        base_block_map=base_map,
        head_block_map=equivalent_map,
        target_ids=frozenset({target["id"]}),
        base_body_md=base_body,
        head_body_md=equivalent_body,
    )
    assert equivalent.outcome == "rebased"
    assert equivalent.changed_ids == frozenset()


def test_unaddressable_operations_always_diverge_when_base_is_stale():
    decision = decide_push_outcome(
        base_version_id=_BASE_ID,
        head_version_id=_HEAD_ID,
        base_block_map=_map(),
        head_block_map=_map(),
        target_ids=None,
    )
    assert decision.outcome == "diverged"


def test_added_or_removed_ids_count_as_changed():
    base = [{"id": "b_1", "kind": "p", "hash": "h1", "lines": 1}]
    head = [
        {"id": "b_1", "kind": "p", "hash": "h1", "lines": 1},
        {"id": "b_2", "kind": "p", "hash": "h2", "lines": 1},
    ]
    decision = decide_push_outcome(
        base_version_id=_BASE_ID,
        head_version_id=_HEAD_ID,
        base_block_map=base,
        head_block_map=head,
        target_ids=frozenset({"b_1"}),
    )
    assert decision.outcome == "rebased"
    assert "b_2" in decision.changed_ids


def test_insert_actual_delta_is_proven_per_anchor_not_by_an_unrelated_insert():
    base_map = [
        {"id": "b_1", "kind": "p", "hash": "h1", "lines": 1},
        {"id": "b_2", "kind": "p", "hash": "h2", "lines": 1},
    ]
    candidate_map = [
        *base_map,
        {"id": "b_new", "kind": "p", "hash": "new", "lines": 1},
    ]
    operations = [
        InsertAfterOperation(
            op="insert_after", block="b_1", expected="0" * 64, content="gone"
        ),
        InsertAfterOperation(
            op="insert_after", block="b_2", expected="0" * 64, content="landed"
        ),
    ]

    assert actual_target_ids_for_operations(
        operations,
        base_block_map=base_map,
        candidate_block_map=candidate_map,
    ) == frozenset({"b_2"})

    base_body = "alpha\ncontinued\n\nbeta\n"
    candidate_body = "alpha  \ncontinued\n\nbeta\n"
    base_map = compute_block_map_with_ids(
        base_body, previous_body_md=None, previous_block_map=None
    )
    candidate_map = compute_block_map_with_ids(
        candidate_body, previous_body_md=base_body, previous_block_map=base_map
    )
    target = base_map[0]
    assert candidate_map[0]["hash"] == target["hash"]
    assert actual_target_ids_for_operations(
        [
            ReplaceBlockOperation(
                op="replace_block",
                block=target["id"],
                expected=target["hash"],
                content="alpha  \ncontinued",
            )
        ],
        base_block_map=base_map,
        candidate_block_map=candidate_map,
        base_body_md=base_body,
        candidate_body_md=candidate_body,
    ) == frozenset({target["id"]})


def test_target_ids_for_operations_collects_block_and_row_ids():
    ops = [
        ReplaceBlockOperation(
            op="replace_block", block="b_1", expected="0" * 64, content="y"
        ),
        InsertAfterOperation(
            op="insert_after", block="b_2", expected="0" * 64, content="y"
        ),
        DeleteBlockOperation(op="delete_block", block="b_3", expected="0" * 64),
        InsertRowOperation(
            op="insert_row", row="r_1", expected="0" * 64, content="| a |"
        ),
    ]
    assert target_ids_for_operations(ops) == frozenset({"b_1", "b_2", "b_3", "r_1"})


def test_target_ids_for_operations_is_none_when_any_op_is_not_block_addressed():
    ops = [
        ReplaceBlockOperation(
            op="replace_block", block="b_1", expected="0" * 64, content="y"
        ),
        ReplaceTextOperation(op="replace_text", old_text="a", new_text="b"),
    ]
    assert target_ids_for_operations(ops) is None


def test_hash_map_includes_both_block_and_row_ids():
    block_map = [
        {
            "id": "b_1",
            "kind": "table",
            "hash": "th",
            "lines": 3,
            "rows": [{"id": "r_1", "hash": "rh1"}, {"id": "r_2", "hash": "rh2"}],
        }
    ]
    assert hash_map(block_map) == {"b_1": "th", "r_1": "rh1", "r_2": "rh2"}


def test_reconciliation_replace_and_delete_convergence_is_already_satisfied():
    base_body = "alpha\ncontinued\n\nbeta\n"
    base_map = compute_block_map_with_ids(
        base_body, previous_body_md=None, previous_block_map=None
    )
    alpha, beta = base_map
    replace_body = "alpha desired\ncontinued\n\nbeta\n"
    replace_map = compute_block_map_with_ids(
        replace_body,
        previous_body_md=base_body,
        previous_block_map=base_map,
    )
    replace = ReplaceBlockOperation(
        op="replace_block",
        block=alpha["id"],
        expected=alpha["hash"],
        content="alpha desired\ncontinued",
    )
    replacement = reconciliation_target_sets(
        operations=[replace],
        base_block_map=base_map,
        authored_body_md=replace_body,
        authored_block_map=replace_map,
        head_block_map=replace_map,
        base_body_md=base_body,
        head_body_md=replace_body,
    )
    assert replacement.conflict_ids == frozenset()
    assert replacement.already_satisfied_ids == frozenset({alpha["id"]})

    different_break_body = "alpha desired  \ncontinued\n\nbeta\n"
    different_break_map = compute_block_map_with_ids(
        different_break_body, previous_body_md=base_body, previous_block_map=base_map
    )
    assert different_break_map[0]["hash"] == replace_map[0]["hash"]
    different_break = reconciliation_target_sets(
        operations=[replace],
        base_block_map=base_map,
        authored_body_md=replace_body,
        authored_block_map=replace_map,
        head_block_map=different_break_map,
        base_body_md=base_body,
        head_body_md=different_break_body,
    )
    assert different_break.conflict_ids == frozenset({alpha["id"]})
    assert different_break.already_satisfied_ids == frozenset()

    delete_body = "beta\n"
    delete_map = compute_block_map_with_ids(
        delete_body,
        previous_body_md=base_body,
        previous_block_map=base_map,
    )
    delete = DeleteBlockOperation(
        op="delete_block",
        block=alpha["id"],
        expected=alpha["hash"],
    )
    deletion = reconciliation_target_sets(
        operations=[delete],
        base_block_map=base_map,
        authored_body_md=delete_body,
        authored_block_map=delete_map,
        head_block_map=delete_map,
        base_body_md=base_body,
        head_body_md=delete_body,
    )
    assert deletion.conflict_ids == frozenset()
    assert deletion.already_satisfied_ids == frozenset({alpha["id"]})
    assert beta["id"] in hash_map(delete_map)


def test_reconciliation_insert_into_changed_gap_remains_a_conflict():
    base_body = "alpha\n\nbeta\n"
    base_map = compute_block_map_with_ids(
        base_body, previous_body_md=None, previous_block_map=None
    )
    alpha = base_map[0]
    authored_body = "alpha\n\ninserted\n\nbeta\n"
    authored_map = compute_block_map_with_ids(
        authored_body,
        previous_body_md=base_body,
        previous_block_map=base_map,
    )
    head_map = compute_block_map_with_ids(
        authored_body,
        previous_body_md=base_body,
        previous_block_map=base_map,
    )
    insertion = reconciliation_target_sets(
        operations=[
            InsertAfterOperation(
                op="insert_after",
                block=alpha["id"],
                expected=alpha["hash"],
                content="inserted",
            )
        ],
        base_block_map=base_map,
        authored_body_md=authored_body,
        authored_block_map=authored_map,
        head_block_map=head_map,
    )
    assert insertion.conflict_ids == frozenset({alpha["id"]})
    assert insertion.already_satisfied_ids == frozenset()


def test_changed_row_id_resolves_only_when_other_rows_prove_same_ordinal():
    base_body = "| a | b |\n|---|---|\n| 1 | 2 |\n| 3 | 4 |\n"
    base_map = compute_block_map_with_ids(
        base_body, previous_body_md=None, previous_block_map=None
    )
    base_table = next(entry for entry in base_map if entry["kind"] == "table")
    old_row_id = base_table["rows"][0]["id"]
    head_body = "| a | b |\n|---|---|\n| 9 | 2 |\n| 3 | 4 |\n"
    head_map = compute_block_map_with_ids(
        head_body, previous_body_md=base_body, previous_block_map=base_map
    )
    head_table = next(entry for entry in head_map if entry["kind"] == "table")

    assert (
        resolve_target_id_between_maps(base_map, head_map, old_row_id)
        == (head_table["rows"][0]["id"])
    )


def test_deleted_row_does_not_resolve_to_the_next_row_shifted_into_its_ordinal():
    base_body = "| a | b |\n|---|---|\n| 1 | 2 |\n| 3 | 4 |\n"
    base_map = compute_block_map_with_ids(
        base_body, previous_body_md=None, previous_block_map=None
    )
    old_row_id = next(entry for entry in base_map if entry["kind"] == "table")["rows"][
        0
    ]["id"]
    head_body = "| a | b |\n|---|---|\n| 3 | 4 |\n"
    head_map = compute_block_map_with_ids(
        head_body, previous_body_md=base_body, previous_block_map=base_map
    )

    assert resolve_target_id_between_maps(base_map, head_map, old_row_id) is None


def test_build_conflict_details_includes_markers_for_a_replace_block_conflict():
    base_body = "# Title\n\nfirst paragraph with shared stable context\n"
    base_map = compute_block_map_with_ids(
        base_body, previous_body_md=None, previous_block_map=None
    )
    paragraph = next(e for e in base_map if e["kind"] == "p")
    head_body = "# Title\n\nfirst paragraph with shared stable context updated\n"
    head_map = compute_block_map_with_ids(
        head_body, previous_body_md=base_body, previous_block_map=base_map
    )

    op = ReplaceBlockOperation(
        op="replace_block",
        block=paragraph["id"],
        expected=paragraph["hash"],
        content="first paragraph with shared stable context, my edit",
    )
    entries = build_conflict_details(
        conflicting_ids=frozenset({paragraph["id"]}),
        base_body_md=base_body,
        base_block_map=base_map,
        head_body_md=head_body,
        head_block_map=head_map,
        head_version_id=_HEAD_ID,
        head_actor_kind="human",
        head_actor_display_name="Kim",
        head_created_at=datetime(2026, 8, 29, tzinfo=UTC),
        operations=[op],
    )

    assert len(entries) == 1
    entry = entries[0]
    assert entry["block_id"] == paragraph["id"]
    assert entry["kind"] == "p"
    assert entry["base"]["text"] == "first paragraph with shared stable context"
    assert entry["head"]["text"] == "first paragraph with shared stable context updated"
    assert entry["head"]["actor_kind"] == "human"
    assert entry["head"]["actor"] == "Kim"
    assert entry["yours"]["text"] == (
        "first paragraph with shared stable context, my edit"
    )
    assert "<<<<<<< yours" in entry["markers"]
    assert "shared stable context, my edit" in entry["markers"]
    assert "shared stable context updated" in entry["markers"]


def test_conflict_text_uses_raw_coordinates_for_cr_only_markdown():
    base_body = "# Title\r\rfirst\r"
    base_map = compute_block_map_with_ids(
        base_body, previous_body_md=None, previous_block_map=None
    )
    paragraph = next(entry for entry in base_map if entry["kind"] == "p")
    head_body = "# Title\r\rfirst?\r"
    head_map = compute_block_map_with_ids(
        head_body, previous_body_md=base_body, previous_block_map=base_map
    )
    entries = build_conflict_details(
        conflicting_ids=frozenset({paragraph["id"]}),
        base_body_md=base_body,
        base_block_map=base_map,
        head_body_md=head_body,
        head_block_map=head_map,
        head_version_id=_HEAD_ID,
        head_actor_kind="human",
        head_actor_display_name="Kim",
        head_created_at=datetime(2026, 8, 29, tzinfo=UTC),
        operations=[
            ReplaceBlockOperation(
                op="replace_block",
                block=paragraph["id"],
                expected=paragraph["hash"],
                content="mine",
            )
        ],
    )

    assert entries[0]["base"]["text"] == "first"
    assert entries[0]["head"]["text"] == "first?"


def test_build_conflict_details_omits_yours_for_delete_block():
    base_body = "# Title\n\nfirst\n"
    base_map = compute_block_map_with_ids(
        base_body, previous_body_md=None, previous_block_map=None
    )
    paragraph = next(e for e in base_map if e["kind"] == "p")
    head_body = "# Title\n\nfirst, changed by someone else\n"
    head_map = compute_block_map_with_ids(
        head_body, previous_body_md=base_body, previous_block_map=base_map
    )

    op = DeleteBlockOperation(
        op="delete_block", block=paragraph["id"], expected=paragraph["hash"]
    )
    entries = build_conflict_details(
        conflicting_ids=frozenset({paragraph["id"]}),
        base_body_md=base_body,
        base_block_map=base_map,
        head_body_md=head_body,
        head_block_map=head_map,
        head_version_id=_HEAD_ID,
        head_actor_kind="mcp_client",
        head_actor_display_name=None,
        head_created_at=datetime(2026, 8, 29, tzinfo=UTC),
        operations=[op],
    )

    assert len(entries) == 1
    assert "yours" not in entries[0]
    assert "markers" not in entries[0]


def test_retired_conflict_marks_deletion_only_when_the_slot_is_proven_empty():
    base_body = "alpha\n\nbeta\n"
    base_map = compute_block_map_with_ids(
        base_body, previous_body_md=None, previous_block_map=None
    )
    alpha = base_map[0]
    head_body = "beta\n"
    head_map = compute_block_map_with_ids(
        head_body, previous_body_md=base_body, previous_block_map=base_map
    )
    entries = build_conflict_details(
        conflicting_ids=frozenset({alpha["id"]}),
        base_body_md=base_body,
        base_block_map=base_map,
        head_body_md=head_body,
        head_block_map=head_map,
        head_version_id=_HEAD_ID,
        head_actor_kind="human",
        head_actor_display_name="Kim",
        head_created_at=datetime(2026, 8, 29, tzinfo=UTC),
        operations=[
            ReplaceBlockOperation(
                op="replace_block",
                block=alpha["id"],
                expected=alpha["hash"],
                content="mine",
            )
        ],
    )

    assert entries[0]["resolution_target_id"] is None
    assert entries[0]["head"]["deleted"] is True
    assert entries[0]["head"]["identity_retired"] is True


def test_retired_conflict_keeps_deletion_unknown_when_a_block_occupies_the_slot():
    base_body = "alpha\n\nbeta\n"
    base_map = compute_block_map_with_ids(
        base_body, previous_body_md=None, previous_block_map=None
    )
    alpha = base_map[0]
    head_body = "unrelated rewritten content\n\nbeta\n"
    head_map = compute_block_map_with_ids(
        head_body, previous_body_md=base_body, previous_block_map=base_map
    )
    entries = build_conflict_details(
        conflicting_ids=frozenset({alpha["id"]}),
        base_body_md=base_body,
        base_block_map=base_map,
        head_body_md=head_body,
        head_block_map=head_map,
        head_version_id=_HEAD_ID,
        head_actor_kind="human",
        head_actor_display_name="Kim",
        head_created_at=datetime(2026, 8, 29, tzinfo=UTC),
        operations=[
            ReplaceBlockOperation(
                op="replace_block",
                block=alpha["id"],
                expected=alpha["hash"],
                content="mine",
            )
        ],
    )

    assert entries[0]["resolution_target_id"] is None
    assert entries[0]["head"]["deleted"] is None
    assert entries[0]["head"]["identity_retired"] is True


def test_build_clean_changes_excludes_conflicting_ids():
    base_body = "# Title\n\nfirst\n\nsecond\n"
    base_map = compute_block_map_with_ids(
        base_body, previous_body_md=None, previous_block_map=None
    )
    first_para = next(e for e in base_map if e["kind"] == "p")
    second_para = [e for e in base_map if e["kind"] == "p"][1]
    head_body = "# Title\n\nfirst changed\n\nsecond changed\n"
    head_map = compute_block_map_with_ids(
        head_body, previous_body_md=base_body, previous_block_map=base_map
    )

    changes = build_clean_changes(
        changed_ids=frozenset({first_para["id"], second_para["id"]}),
        conflicting_ids=frozenset({first_para["id"]}),
        head_body_md=head_body,
        head_block_map=head_map,
    )

    assert [entry["block_id"] for entry in changes] == [second_para["id"]]
    assert changes[0]["summary"] == "second changed"

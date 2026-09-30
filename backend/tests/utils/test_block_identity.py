import pytest

import backend.utils.block_identity as block_identity_module
from backend.utils.block_identity import (
    BlockMapTransitionError,
    BlockOperationProvenance,
    block_map_delta,
    block_map_for_version,
    compute_block_map_with_ids,
    validated_block_map_for_version,
)
from backend.utils.content_hash import content_hash

pytestmark = pytest.mark.unit


def _ids(block_map: list[dict]) -> list[str]:
    return [entry["id"] for entry in block_map]


def test_strict_block_map_loader_rejects_stale_position_cache_fields():
    body = "# Title\n\nparagraph\n"
    block_map = compute_block_map_with_ids(
        body,
        previous_body_md=None,
        previous_block_map=None,
    )
    stale = [dict(entry) for entry in block_map]
    stale[1]["gap"] += 1

    assert validated_block_map_for_version(block_map, body) is block_map
    with pytest.raises(BlockMapTransitionError):
        validated_block_map_for_version(stale, body)


def test_block_map_delta_reports_block_and_row_identity_transitions():
    before = [
        {
            "id": "b_table",
            "kind": "table",
            "hash": "a" * 64,
            "rows": [{"id": "r_one", "hash": "b" * 64}],
        },
        {"id": "b_removed", "kind": "p", "hash": "c" * 64},
    ]
    after = [
        {
            "id": "b_table",
            "kind": "table",
            "hash": "d" * 64,
            "rows": [
                {"id": "r_one", "hash": "e" * 64},
                {"id": "r_added", "hash": "f" * 64},
            ],
        },
        {"id": "b_added", "kind": "p", "hash": "0" * 64},
    ]

    delta = block_map_delta(before, after)

    assert [entry["change"] for entry in delta] == [
        "replaced",
        "replaced",
        "added",
        "added",
        "removed",
    ]
    assert delta[0]["old"]["id"] == delta[0]["new"]["id"] == "b_table"
    assert delta[1]["new"]["parent_block_id"] == "b_table"
    assert delta[2]["new"] == {
        "id": "r_added",
        "hash": "f" * 64,
        "node_type": "row",
        "kind": None,
        "parent_block_id": "b_table",
    }
    assert delta[-1]["old"]["id"] == "b_removed"


@pytest.mark.parametrize(
    "malformed",
    (
        ["not-a-block"],
        [
            {
                "id": "b_table",
                "kind": "table",
                "hash": "a" * 64,
                "rows": ["not-a-row"],
            }
        ],
    ),
)
def test_block_map_delta_rejects_malformed_durable_json_without_attribute_error(
    malformed,
):
    with pytest.raises(BlockMapTransitionError):
        block_map_delta(malformed, [])


def test_first_version_mints_a_fresh_id_for_every_block():
    body = "# Title\n\nfirst\n\nsecond\n"
    block_map = compute_block_map_with_ids(
        body, previous_body_md=None, previous_block_map=None
    )

    ids = _ids(block_map)
    assert len(ids) == 3
    assert len(set(ids)) == 3
    assert all(entry["id"].startswith("b_") for entry in block_map)


def test_unrelated_edit_elsewhere_keeps_every_untouched_id():
    before = "# A\n\nfirst\n\n# B\n\nsecond\n\n# C\n\nthird\n"
    after = (
        "# A\n\nfirst, edited with much more text now\n\n"
        "# B\n\nsecond\n\n# C\n\nthird\n"
    )

    before_map = compute_block_map_with_ids(
        before, previous_body_md=None, previous_block_map=None
    )
    after_map = compute_block_map_with_ids(
        after, previous_body_md=before, previous_block_map=before_map
    )

    # A's paragraph changed -> new id; every other block's id is unchanged.
    assert before_map[0]["id"] == after_map[0]["id"]  # "# A"
    assert before_map[1]["id"] != after_map[1]["id"]  # edited paragraph
    assert before_map[2]["id"] == after_map[2]["id"]  # "# B"
    assert before_map[3]["id"] == after_map[3]["id"]  # "second"
    assert before_map[4]["id"] == after_map[4]["id"]  # "# C"
    assert before_map[5]["id"] == after_map[5]["id"]  # "third"


def test_a_lightly_edited_paragraph_keeps_its_id_via_fuzzy_match():
    before = (
        "# Title\n\nThe quick brown fox jumps over the lazy dog "
        "and keeps running for a while.\n"
    )
    after = (
        "# Title\n\nThe quick brown fox jumps over the very lazy dog "
        "and keeps running for a while.\n"
    )

    before_map = compute_block_map_with_ids(
        before, previous_body_md=None, previous_block_map=None
    )
    after_map = compute_block_map_with_ids(
        after, previous_body_md=before, previous_block_map=before_map
    )

    assert before_map[1]["id"] == after_map[1]["id"]
    assert before_map[1]["hash"] != after_map[1]["hash"]


def test_a_rewritten_paragraph_gets_a_new_id_not_a_false_match():
    before = "# Title\n\nThe quick brown fox jumps over the lazy dog.\n"
    after = "# Title\n\nCompletely unrelated content about quarterly revenue.\n"

    before_map = compute_block_map_with_ids(
        before, previous_body_md=None, previous_block_map=None
    )
    after_map = compute_block_map_with_ids(
        after, previous_body_md=before, previous_block_map=before_map
    )

    assert before_map[1]["id"] != after_map[1]["id"]


def test_ambiguous_identical_blocks_get_fresh_ids_across_a_reorder():
    before = "# Title\n\nsame text\n\nsame text\n\nunique tail\n"
    after = "# Title\n\nunique tail\n\nsame text\n\nsame text\n"

    before_map = compute_block_map_with_ids(
        before, previous_body_md=None, previous_block_map=None
    )
    after_map = compute_block_map_with_ids(
        after, previous_body_md=before, previous_block_map=before_map
    )

    before_ids = _ids(before_map)
    after_ids = _ids(after_map)
    assert len(after_ids) == 4
    assert len(set(after_ids)) == 4
    assert after_ids[0] == before_ids[0]
    assert after_ids[1] == before_ids[3]
    assert set(after_ids[2:]).isdisjoint(before_ids)


def test_a_deleted_block_is_simply_absent_from_the_new_map():
    before = "# A\n\nfirst\n\n# B\n\nsecond\n"
    after = "# A\n\nfirst\n"

    before_map = compute_block_map_with_ids(
        before, previous_body_md=None, previous_block_map=None
    )
    after_map = compute_block_map_with_ids(
        after, previous_body_md=before, previous_block_map=before_map
    )

    assert _ids(after_map) == [before_map[0]["id"], before_map[1]["id"]]


def test_unchanged_table_rows_keep_their_ids_when_one_row_changes():
    before = "| a | b |\n|---|---|\n| 1 | 2 |\n| 3 | 4 |\n"
    after = "| a | b |\n|---|---|\n| 1 | 2 |\n| 9 | 9 |\n"

    before_map = compute_block_map_with_ids(
        before, previous_body_md=None, previous_block_map=None
    )
    after_map = compute_block_map_with_ids(
        after, previous_body_md=before, previous_block_map=before_map
    )

    before_table = before_map[0]
    after_table = after_map[0]
    assert before_table["id"] == after_table["id"]
    assert before_table["rows"][0]["id"] == after_table["rows"][0]["id"]
    assert before_table["rows"][1]["id"] != after_table["rows"][1]["id"]


def test_a_misaligned_stored_map_falls_back_to_fresh_ids_for_everything():
    before = "# A\n\nfirst\n"
    after = "# A\n\nfirst\n\n# B\n\nsecond\n"
    corrupt_previous_map = [{"id": "b_only_one", "kind": "h1", "hash": "x", "lines": 1}]

    after_map = compute_block_map_with_ids(
        after, previous_body_md=before, previous_block_map=corrupt_previous_map
    )

    assert len(_ids(after_map)) == 4
    assert len(set(_ids(after_map))) == 4


def test_recomputing_an_unambiguous_carry_forward_is_deterministic():
    # Determinism only holds where every block resolves via tier 1/2 (a
    # confident match) -- a genuine tier-3 fresh mint is an intentionally
    # random token and is not expected to repeat across separate calls.
    before = (
        "# Title\n\nThe quick brown fox jumps over the lazy dog "
        "and keeps running for a while.\n"
    )
    after = (
        "# Title\n\nThe quick brown fox jumps over the very lazy dog "
        "and keeps running for a while.\n"
    )
    before_map = compute_block_map_with_ids(
        before, previous_body_md=None, previous_block_map=None
    )

    after_map_1 = compute_block_map_with_ids(
        after, previous_body_md=before, previous_block_map=before_map
    )
    after_map_2 = compute_block_map_with_ids(
        after, previous_body_md=before, previous_block_map=before_map
    )

    assert [entry["id"] for entry in after_map_1] == [
        entry["id"] for entry in after_map_2
    ]


def test_explicit_replacement_keeps_the_id_past_the_similarity_threshold():
    before = "# Title\n\nfirst\n"
    after = "# Title\n\nCompletely unrelated content about quarterly revenue.\n"
    before_map = compute_block_map_with_ids(
        before, previous_body_md=None, previous_block_map=None
    )
    before_paragraph = before_map[1]

    # Without the hint, section 2.2's generic-diff tier correctly refuses
    # to guess (this exact scenario is test_a_rewritten_paragraph_gets_a_
    # new_id_not_a_false_match above).
    without_hint = compute_block_map_with_ids(
        after, previous_body_md=before, previous_block_map=before_map
    )
    assert without_hint[1]["id"] != before_paragraph["id"]

    # With the hint (what a block-addressed replace_block op supplies),
    # identity survives even though content similarity does not.
    with_hint = compute_block_map_with_ids(
        after,
        previous_body_md=before,
        previous_block_map=before_map,
        explicit_replacements={
            before_paragraph["id"]: content_hash(
                "Completely unrelated content about quarterly revenue.\n"
            )
        },
    )
    assert with_hint[1]["id"] == before_paragraph["id"]


def test_explicit_replacement_does_not_depend_on_a_pre_normalization_hash():
    before = "# Title\n\nfirst\n"
    after = "# Title\n\nsomething else entirely and unrelated to first\n"
    before_map = compute_block_map_with_ids(
        before, previous_body_md=None, previous_block_map=None
    )
    before_paragraph = before_map[1]

    result = compute_block_map_with_ids(
        after,
        previous_body_md=before,
        previous_block_map=before_map,
        explicit_replacements={before_paragraph["id"]: "0" * 64},
    )

    # The old compatibility payload carries a derived hash, but the named
    # target is the provenance fact. Normalization is allowed to change the
    # final hash without losing the target's identity.
    assert result[1]["id"] == before_paragraph["id"]


def test_block_map_for_version_computes_deterministic_wide_legacy_ids():
    body = "# Title\n\nfirst\n"
    computed = block_map_for_version(None, body)

    assert [entry["kind"] for entry in computed] == ["h1", "p"]
    assert all(entry["id"].startswith("b_") for entry in computed)
    assert all(len(entry["id"].removeprefix("b_")) >= 32 for entry in computed)
    assert len({entry["id"] for entry in computed}) == 2
    assert block_map_for_version(None, body) == computed


def test_block_map_for_version_trusts_a_stored_value_without_recomputing():
    sentinel = [{"id": "b_stored", "kind": "p", "hash": "not-recomputed", "lines": 1}]
    assert block_map_for_version(sentinel, "# Title\n\nfirst\n") == sentinel


def test_insert_after_provenance_does_not_steal_an_identical_old_block_id():
    before = "# Title\n\nanchor\n\nsame\n"
    after = "# Title\n\nanchor\n\nsame\n\nsame\n"
    before_map = compute_block_map_with_ids(
        before, previous_body_md=None, previous_block_map=None
    )

    after_map = compute_block_map_with_ids(
        after,
        previous_body_md=before,
        previous_block_map=before_map,
        operation_provenance=(
            BlockOperationProvenance("insert_after", before_map[1]["id"], 1),
        ),
    )

    assert after_map[1]["id"] == before_map[1]["id"]
    assert after_map[2]["id"] not in _ids(before_map)
    assert after_map[3]["id"] == before_map[2]["id"]


def test_insert_before_provenance_restores_content_before_a_surviving_successor():
    before = "first\n\nsecond\n"
    after = "restored\n\nfirst\n\nsecond\n"
    before_map = compute_block_map_with_ids(
        before, previous_body_md=None, previous_block_map=None
    )

    after_map = compute_block_map_with_ids(
        after,
        previous_body_md=before,
        previous_block_map=before_map,
        operation_provenance=(
            BlockOperationProvenance("insert_before", before_map[0]["id"], 1),
        ),
        identity_seed="restore-before",
    )

    assert after_map[0]["id"] not in _ids(before_map)
    assert after_map[1]["id"] == before_map[0]["id"]
    assert after_map[2]["id"] == before_map[1]["id"]


def test_insert_document_provenance_is_only_valid_for_an_empty_map():
    empty_map = compute_block_map_with_ids(
        "", previous_body_md=None, previous_block_map=None
    )
    operation = BlockOperationProvenance("insert_document", None, 2)

    restored = compute_block_map_with_ids(
        "first\n\nsecond\n",
        previous_body_md="",
        previous_block_map=empty_map,
        operation_provenance=(operation,),
        identity_seed="restore-document",
    )

    assert len(restored) == 2
    assert len(set(_ids(restored))) == 2

    nonempty = compute_block_map_with_ids(
        "existing\n", previous_body_md=None, previous_block_map=None
    )
    with pytest.raises(BlockMapTransitionError):
        compute_block_map_with_ids(
            "first\n\nsecond\n",
            previous_body_md="existing\n",
            previous_block_map=nonempty,
            operation_provenance=(operation,),
        )


def test_delete_provenance_removes_the_named_identical_block_only():
    before = "# Title\n\nsame\n\nsame\n"
    after = "# Title\n\nsame\n"
    before_map = compute_block_map_with_ids(
        before, previous_body_md=None, previous_block_map=None
    )

    after_map = compute_block_map_with_ids(
        after,
        previous_body_md=before,
        previous_block_map=before_map,
        operation_provenance=(
            BlockOperationProvenance("delete_block", before_map[1]["id"]),
        ),
    )

    assert _ids(after_map) == [before_map[0]["id"], before_map[2]["id"]]


def test_insert_row_provenance_keeps_duplicate_old_rows_by_position():
    before = "| a | b |\n|---|---|\n| 1 | 2 |\n| 1 | 2 |\n"
    after = "| a | b |\n|---|---|\n| 1 | 2 |\n| 1 | 2 |\n| 1 | 2 |\n"
    before_map = compute_block_map_with_ids(
        before, previous_body_md=None, previous_block_map=None
    )
    old_rows = before_map[0]["rows"]

    after_map = compute_block_map_with_ids(
        after,
        previous_body_md=before,
        previous_block_map=before_map,
        operation_provenance=(
            BlockOperationProvenance("insert_row", old_rows[0]["id"], 1),
        ),
    )
    new_rows = after_map[0]["rows"]

    assert new_rows[0]["id"] == old_rows[0]["id"]
    assert new_rows[1]["id"] not in {row["id"] for row in old_rows}
    assert new_rows[2]["id"] == old_rows[1]["id"]


def test_replace_provenance_survives_final_table_padding_normalization():
    before = "| a | b |\n|---|---|\n| 1 | 2 |\n"
    after = "| a   | b   |\n| --- | --- |\n| 9   | 9   |\n"
    before_map = compute_block_map_with_ids(
        before, previous_body_md=None, previous_block_map=None
    )

    after_map = compute_block_map_with_ids(
        after,
        previous_body_md=before,
        previous_block_map=before_map,
        operation_provenance=(
            BlockOperationProvenance("replace_block", before_map[0]["id"]),
        ),
    )

    assert after_map[0]["id"] == before_map[0]["id"]


def test_multi_block_replace_transition_fails_closed():
    before = "# Title\n\nfirst\n"
    after = "# Title\n\none\n\ntwo\n"
    before_map = compute_block_map_with_ids(
        before, previous_body_md=None, previous_block_map=None
    )

    with pytest.raises(BlockMapTransitionError):
        compute_block_map_with_ids(
            after,
            previous_body_md=before,
            previous_block_map=before_map,
            operation_provenance=(
                BlockOperationProvenance("replace_block", before_map[1]["id"]),
            ),
        )


def test_stable_identity_seed_reuses_dry_run_ids_and_separates_requests():
    before = "# Title\n\nfirst\n"
    after = "# Title\n\nfirst\n\nsecond\n"
    before_map = compute_block_map_with_ids(
        before, previous_body_md=None, previous_block_map=None
    )
    provenance = (BlockOperationProvenance("insert_after", before_map[1]["id"], 1),)

    dry_run = compute_block_map_with_ids(
        after,
        previous_body_md=before,
        previous_block_map=before_map,
        operation_provenance=provenance,
        identity_seed="stable-request-a",
    )
    commit = compute_block_map_with_ids(
        after,
        previous_body_md=before,
        previous_block_map=before_map,
        operation_provenance=provenance,
        identity_seed="stable-request-a",
    )
    other_request = compute_block_map_with_ids(
        after,
        previous_body_md=before,
        previous_block_map=before_map,
        operation_provenance=provenance,
        identity_seed="stable-request-b",
    )

    assert dry_run == commit
    assert dry_run[2]["id"] != other_request[2]["id"]


def test_fuzzy_matching_uses_a_fixed_candidate_window(monkeypatch):
    calls = 0
    real_matcher = block_identity_module.SequenceMatcher

    def counting_matcher(*args, **kwargs):
        nonlocal calls
        calls += 1
        return real_matcher(*args, **kwargs)

    monkeypatch.setattr(block_identity_module, "SequenceMatcher", counting_matcher)
    before = "\n\n".join(
        f"old paragraph {index} with some repeated context" for index in range(80)
    )
    after = "\n\n".join(
        f"edited paragraph {index} with different context" for index in range(80)
    )
    before_map = compute_block_map_with_ids(
        before, previous_body_md=None, previous_block_map=None
    )

    compute_block_map_with_ids(
        after, previous_body_md=before, previous_block_map=before_map
    )

    assert calls <= 80 * 25


_LABELLED = (
    "# Plan\n\n"
    "## A\n\nAcceptance criteria:\n\n- a one\n\n"
    "## B\n\nAcceptance criteria:\n\n- b one\n\n"
    "## C\n\nAcceptance criteria:\n\n- c one\n"
)


def _id_after(block_map: list[dict], body: str, heading: str) -> str:
    """The id of the block right after `heading` (its repeated label)."""

    from backend.utils.block_map import compute_blocks

    blocks = compute_blocks(body)
    lines = body.split("\n")
    for index, block in enumerate(blocks):
        if lines[block.line_start] == heading:
            return block_map[index + 1]["id"]
    raise AssertionError(heading)


def _carry(before: str, after: str) -> tuple[list[dict], list[dict]]:
    before_map = compute_block_map_with_ids(
        before, previous_body_md=None, previous_block_map=None, identity_seed="s"
    )
    after_map = compute_block_map_with_ids(
        after,
        previous_body_md=before,
        previous_block_map=before_map,
        identity_seed="s",
    )
    return before_map, after_map


def test_repeated_labels_keep_their_ids_across_an_unrelated_edit():
    # Every in-place write recomputes the map, so identical blocks (a label
    # repeated under each item) lost their ids on EVERY edit anywhere, and an
    # id read before the edit no longer resolved.
    for after in (
        _LABELLED + "\n## D\n\nA new section at the end.\n",
        _LABELLED.replace("- b one", "- b one, edited"),
    ):
        before_map, after_map = _carry(_LABELLED, after)
        for heading in ("## A", "## B", "## C"):
            assert _id_after(after_map, after, heading) == _id_after(
                before_map, _LABELLED, heading
            ), heading


def test_a_repeated_label_follows_its_own_section_when_sections_swap():
    # Pairing identical blocks by order would hand A's label id to C's label
    # after the swap, and a block-addressed edit would land in the wrong
    # section. The neighbour decides, not the order.
    swapped = (
        "# Plan\n\n"
        "## C\n\nAcceptance criteria:\n\n- c one\n\n"
        "## B\n\nAcceptance criteria:\n\n- b one\n\n"
        "## A\n\nAcceptance criteria:\n\n- a one\n"
    )
    before_map, after_map = _carry(_LABELLED, swapped)
    for heading in ("## A", "## B", "## C"):
        assert _id_after(after_map, swapped, heading) == _id_after(
            before_map, _LABELLED, heading
        ), heading


def test_deleting_one_labelled_section_leaves_the_others_their_own_ids():
    without_b = _LABELLED.replace("## B\n\nAcceptance criteria:\n\n- b one\n\n", "")
    before_map, after_map = _carry(_LABELLED, without_b)
    for heading in ("## A", "## C"):
        assert _id_after(after_map, without_b, heading) == _id_after(
            before_map, _LABELLED, heading
        ), heading
    assert len({entry["id"] for entry in after_map}) == len(after_map)


def test_a_run_of_identical_blocks_is_pinned_from_its_anchor():
    before = "# T\n\n## Log\n\nsame\n\nsame\n\nsame\n\n## End\n\ntail\n"
    after = before.replace("tail", "tail, edited")
    before_map, after_map = _carry(before, after)
    assert [e["id"] for e in after_map[2:5]] == [e["id"] for e in before_map[2:5]]


def test_a_duplicated_paragraph_keeps_the_original_id_on_the_original():
    before = "# T\n\nfirst\n\nkeep me\n\nlast\n"
    after = "# T\n\nfirst\n\nkeep me\n\nkeep me\n\nlast\n"
    before_map, after_map = _carry(before, after)
    assert after_map[2]["id"] == before_map[2]["id"]
    assert after_map[3]["id"] not in {e["id"] for e in before_map}
    assert after_map[4]["id"] == before_map[3]["id"]


_LABEL = "**Acceptance criteria:**"
_GROUPS = (
    f"# Spec\n\n{_LABEL}\n\n- login works for alpha\n\n{_LABEL}\n\n"
    f"- export works for beta\n\n{_LABEL}\n\n- sync works for gamma\n"
)


def _label_ids(block_map: list[dict]) -> list[str]:
    return [entry["id"] for entry in block_map if entry["kind"] == "p"]


def test_a_label_id_never_moves_to_another_group_when_groups_swap():
    # B-6. Every group has the same label, and the alpha and beta groups
    # swap. Each label's two matched neighbours now name different old
    # labels; trusting the predecessor alone handed the gamma label's id to
    # the label in front of alpha, so an edit by that id changed the wrong
    # group. Disagreeing neighbours prove nothing: the labels get new ids.
    swapped = (
        f"# Spec\n\n{_LABEL}\n\n- export works for beta\n\n{_LABEL}\n\n"
        f"- login works for alpha\n\n{_LABEL}\n\n- sync works for gamma\n"
    )
    before_map, after_map = _carry(_GROUPS, swapped)
    old_labels = set(_label_ids(before_map))
    assert old_labels.isdisjoint(_label_ids(after_map))
    # The lists themselves are unique and keep their ids.
    assert after_map[2]["id"] == before_map[4]["id"]
    assert after_map[4]["id"] == before_map[2]["id"]
    assert after_map[6]["id"] == before_map[6]["id"]


def test_one_neighbour_vouches_only_if_it_stayed_put():
    # The beta group moved up and the alpha list was rewritten. The label
    # after the moved beta list has ONE matched neighbour, and that list sat
    # elsewhere before: following it handed the gamma label's id to the
    # label in front of the rewritten list. The gamma label, whose matched
    # neighbour (the last block) did not move, keeps its own id.
    rewritten = (
        f"# Spec\n\n{_LABEL}\n\n- export works for beta\n\n{_LABEL}\n\n"
        f"- totally rewritten list\n\n{_LABEL}\n\n- sync works for gamma\n"
    )
    before_map, after_map = _carry(_GROUPS, rewritten)
    gamma_label = _label_ids(before_map)[2]
    assert _label_ids(after_map)[2] == gamma_label
    assert gamma_label not in _label_ids(after_map)[:2]


def test_a_successor_alone_can_pin_a_block():
    # The paragraph before the first label was rewritten, so only the list
    # after it can say which label this is (once the second label, which
    # both of its neighbours agree on, shows that list did not move).
    before = (
        f"# T\n\nOld intro paragraph.\n\n{_LABEL}\n\n- first list\n\n"
        f"{_LABEL}\n\n- second list\n"
    )
    after = before.replace(
        "Old intro paragraph.", "A completely different opening sentence."
    )
    before_map, after_map = _carry(before, after)
    assert after_map[1]["id"] != before_map[1]["id"]
    assert _label_ids(after_map)[1:] == _label_ids(before_map)[1:]


def test_a_heading_is_pinned_by_its_own_section_only():
    # Identical headings. The first section's body is new and the alpha body
    # moved under the second heading. The block before a heading belongs to
    # the previous section, so it cannot say which "## Notes" this is; the
    # heading that used to open the alpha section must not stay on top.
    before = "# T\n\n## Notes\n\nalpha body text\n\n## Notes\n\nbeta body text\n"
    after = "# T\n\n## Notes\n\na brand new body here\n\n## Notes\n\nalpha body text\n"
    before_map, after_map = _carry(before, after)
    assert after_map[1]["id"] != before_map[1]["id"]


def test_a_heading_never_vouches_for_the_block_before_it():
    # Sections B and C swapped and both notes paragraphs were rewritten.
    # The "See also" ending section A is followed by heading C, and the block
    # before heading C used to be B's "See also": a heading opens the next
    # section, so it cannot say which section's line comes before it.
    before = (
        "# T\n\n## A\n\nalpha notes\n\nSee also\n\n"
        "## B\n\nbeta notes\n\nSee also\n\n## C\n\ngamma notes\n"
    )
    after = (
        "# T\n\n## A\n\nalpha notes rewritten completely differently\n\n"
        "See also\n\n## C\n\ngamma notes\n\n"
        "## B\n\nbeta notes also rewritten from scratch\n\nSee also\n"
    )
    before_map, after_map = _carry(before, after)
    b_see_also = before_map[6]["id"]
    assert after_map[3]["id"] != b_see_also
    # The headings and the unchanged paragraph keep their ids.
    assert [after_map[index]["id"] for index in (1, 4, 5, 6)] == [
        before_map[index]["id"] for index in (1, 7, 8, 4)
    ]

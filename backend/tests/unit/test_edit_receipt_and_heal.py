"""Edit receipts and block-map healing: correctness and bounded cost.

Both run on every MCP edit, and both first shipped with a sequence diff that
went quadratic on large, repetitive bodies: 97 s (receipt) and 19 s (heal) on
a 900 KB full rewrite. The time bounds below are generous on purpose; they
exist to catch a return to quadratic behaviour, not to benchmark.
"""

from time import perf_counter

from backend.services.edit_receipt import (
    MAX_CHANGES,
    MAX_DIFF_WORK,
    _change_regions,
    _changed_spans,
    build_edit_receipt,
)
from backend.utils.block_identity import compute_block_map_with_ids, heal_block_map


def _repetitive_body(sections: int, word: str = "word") -> str:
    return ("## Same heading\n\n" + f"{word} " * 60 + "\n\n") * sections


def test_heal_keeps_ids_of_unchanged_blocks_and_is_deterministic():
    body = "# Title\n\nOne.\n\nTwo.\n\nThree."
    stale = compute_block_map_with_ids(
        "# Title\n\nOld one.\n\nTwo.\n\nThree.",
        previous_body_md=None,
        previous_block_map=None,
    )
    healed, repaired = heal_block_map(body, stale, identity_seed="row")
    again, _ = heal_block_map(body, stale, identity_seed="row")

    assert repaired is True
    assert healed == again
    ids_by_hash = {entry["hash"]: entry["id"] for entry in stale}
    kept = [entry for entry in healed if entry["hash"] in ids_by_hash]
    assert {entry["id"] for entry in kept} == {
        ids_by_hash[entry["hash"]] for entry in kept
    }
    assert len({entry["id"] for entry in healed}) == len(healed)
    # A valid map is returned untouched.
    valid = compute_block_map_with_ids(
        body, previous_body_md=None, previous_block_map=None
    )
    assert heal_block_map(body, valid, identity_seed="row") == (valid, False)


def test_heal_carries_identical_blocks_only_when_the_group_sizes_match():
    body = "## Same\n\nA.\n\n## Same\n\nB."
    stale = compute_block_map_with_ids(
        "## Same\n\nX.\n\n## Same\n\nY.", previous_body_md=None, previous_block_map=None
    )
    healed, _ = heal_block_map(body, stale, identity_seed="row")
    # Two "## Same" on each side: carried in order.
    assert healed[0]["id"] == stale[0]["id"]
    assert healed[2]["id"] == stale[2]["id"]

    three = body + "\n\n## Same\n\nC."
    healed_three, _ = heal_block_map(three, stale, identity_seed="row")
    stale_ids = {entry["id"] for entry in stale}
    # Three against two is ambiguous: no heading inherits an old id.
    headings = [entry for entry in healed_three if entry["kind"] == "h2"]
    assert all(entry["id"] not in stale_ids for entry in headings)


def test_heal_of_a_large_repetitive_rewrite_is_not_quadratic():
    before = _repetitive_body(900)
    stale = compute_block_map_with_ids(
        before, previous_body_md=None, previous_block_map=None
    )
    after = _repetitive_body(900, word="term")

    started = perf_counter()
    heal_block_map(after, stale, identity_seed="row")

    assert perf_counter() - started < 5


def test_receipt_of_a_large_rewrite_is_bounded_and_keeps_exact_sizes():
    before = _repetitive_body(900)
    after = _repetitive_body(900, word="term")

    started = perf_counter()
    receipt = build_edit_receipt(
        edit_mode="whole_body",
        before_body=before,
        after_body=after,
        editing_revision_before=3,
        editing_revision_after=4,
    )

    assert perf_counter() - started < 5
    spans = _changed_spans(before, after)
    before_lines = before.count("\n")
    assert before_lines * before_lines > MAX_DIFF_WORK
    assert len(spans) == 1
    b1, b2, a1, a2 = spans[0]
    assert before[:b1] + after[a1:a2] + before[b2:] == after
    assert receipt.chars_removed == b2 - b1
    assert receipt.chars_added == a2 - a1
    # The one merged region is far past the byte cap: left out whole, counted.
    assert receipt.changes == []
    assert receipt.changes_omitted_count == 1

    edits = [f"line {index}" for index in range(MAX_CHANGES + 5)]
    many = build_edit_receipt(
        edit_mode="whole_body",
        before_body="\n\n".join(edits),
        after_body="\n\n".join(f"{edit} edited" for edit in edits),
        editing_revision_before=4,
        editing_revision_after=5,
    )
    assert [change.added for change in many.changes] == [" edited"] * MAX_CHANGES
    assert many.changes_omitted_count == 5


def test_changed_spans_always_reconstruct_the_new_body():
    import random

    generator = random.Random(7)
    for _ in range(1000):
        before = "".join(
            generator.choice("ab\r\n") for _ in range(generator.randint(0, 30))
        )
        after = "".join(
            generator.choice("ab\r\n") for _ in range(generator.randint(0, 30))
        )
        spans = _changed_spans(before, after)
        rebuilt: list[str] = []
        cursor = 0
        for b1, b2, a1, a2 in spans:
            assert b1 >= cursor
            rebuilt.append(before[cursor:b1])
            rebuilt.append(after[a1:a2])
            cursor = b2
        rebuilt.append(before[cursor:])
        assert "".join(rebuilt) == after

        # The receipt's regions alone rebuild it too, quoting exact text with
        # same-line context on both sides.
        receipt = build_edit_receipt(
            edit_mode="whole_body",
            before_body=before,
            after_body=after,
            editing_revision_before=1,
            editing_revision_after=2,
        )
        assert receipt.changes_omitted_count == 0
        regions = _change_regions(before, spans, after)
        rebuilt = []
        cursor = 0
        for (b1, b2, a1, a2), change in zip(regions, receipt.changes, strict=True):
            assert change.removed == before[b1:b2]
            assert change.added == after[a1:a2]
            assert after[:a1].endswith(change.context_before)
            assert after[a2:].startswith(change.context_after)
            assert not {"\r", "\n"} & set(change.context_before + change.context_after)
            rebuilt.append(before[cursor:b1])
            rebuilt.append(change.added)
            cursor = b2
        rebuilt.append(before[cursor:])
        assert "".join(rebuilt) == after


def test_an_inserted_section_is_attributed_to_itself_not_the_one_above():
    receipt = build_edit_receipt(
        edit_mode="whole_body",
        before_body="# Doc\n\n## A\n\nKeep.\n",
        after_body="# Doc\n\n## A\n\nKeep.\n\n## B\n\nNew.\n",
        editing_revision_before=1,
        editing_revision_after=2,
    )

    assert receipt.changed_sections == [["Doc", "B"]]
    assert receipt.changes[0].added.lstrip("\n").startswith("## B")

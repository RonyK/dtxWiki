import pytest

from backend.utils.block_map import compute_blocks, serialize_blocks
from backend.utils.content_hash import content_hash

pytestmark = pytest.mark.unit

_DOC = (
    "---\ntitle: Demo\n---\n\n"
    "# Title\n\n"
    "Intro paragraph.\n\n"
    "## Section\n\n"
    "Some text here.\n\n"
    "- item one\n- item two\n\n"
    "> a quote\n\n"
    "| a | b |\n|---|---|\n| 1 | 2 |\n| 3 | 4 |\n\n"
    "```python\ncode here\n```\n\n"
    "<div>raw html</div>\n\n"
    "---\n\n"
    "Trailing para.\n"
)


def test_block_map_covers_every_block_kind_and_excludes_frontmatter():
    blocks = compute_blocks(_DOC)

    assert [block.kind for block in blocks] == [
        "h1",
        "p",
        "h2",
        "p",
        "list",
        "blockquote",
        "table",
        "code",
        "html",
        "thematic_break",
        "p",
    ]
    # The frontmatter's own "---" delimiters produced no block of their own --
    # only the later, unrelated "---" (a real thematic break) counts.
    assert sum(1 for block in blocks if block.kind == "thematic_break") == 1


def test_block_hash_matches_the_normalized_content_hash_of_its_own_lines():
    blocks = compute_blocks(_DOC)
    intro = next(
        block
        for block in blocks
        if block.kind == "p" and block.lines == 1 and block.sub is None
    )
    lines = _DOC.split("\n")
    raw_slice = "\n".join(lines[intro.line_start : intro.line_end])
    assert intro.hash == content_hash(raw_slice)


def test_table_block_carries_exactly_its_data_rows_not_header_or_separator():
    blocks = compute_blocks(_DOC)
    table = next(block for block in blocks if block.kind == "table")

    assert len(table.rows) == 2
    assert table.rows[0].hash != table.rows[1].hash


def test_table_row_hash_is_stable_under_padding_and_outer_pipe_changes():
    padded = "| a  |  b  |\n| :- | --: |\n|  1 |   2 |\n"
    unpadded = "a|b\n:-|--:\n1|2\n"

    padded_table = next(
        b for b in compute_blocks(f"# T\n\n{padded}") if b.kind == "table"
    )
    unpadded_table = next(
        b for b in compute_blocks(f"# T\n\n{unpadded}") if b.kind == "table"
    )

    assert [row.hash for row in padded_table.rows] == [
        row.hash for row in unpadded_table.rows
    ]


def test_heading_subtree_hash_changes_with_a_child_but_not_with_a_sibling():
    base = "# Parent\n\nintro\n\n## Child\n\nchild text\n\n# Next\n\nnext text\n"
    child_edited = "# Parent\n\nintro\n\n## Child\n\nCHANGED\n\n# Next\n\nnext text\n"
    sibling_edited = "# Parent\n\nintro\n\n## Child\n\nchild text\n\n# Next\n\nCHANGED"

    base_parent = next(b for b in compute_blocks(base) if b.kind == "h1")
    child_edited_parent = next(
        b for b in compute_blocks(child_edited) if b.kind == "h1"
    )
    sibling_edited_parent = next(
        b for b in compute_blocks(sibling_edited) if b.kind == "h1"
    )

    assert base_parent.sub != child_edited_parent.sub
    assert base_parent.sub == sibling_edited_parent.sub
    # The parent heading BLOCK itself ("# Parent" alone) never changed --
    # only its subtree fold did.
    assert base_parent.hash == child_edited_parent.hash == sibling_edited_parent.hash


def test_unrelated_block_hash_and_lines_survive_an_edit_elsewhere():
    # Section 2.2/3.3's carry-forward premise: a block untouched by an edit
    # keeps the exact same hash and lines, regardless of what else in the
    # document moved around it.
    before = "# A\n\nfirst\n\n# B\n\nsecond\n\n# C\n\nthird\n"
    after = "# A\n\nfirst, now longer with words\n\n# B\n\nsecond\n\n# C\n\nthird\n"

    before_blocks = compute_blocks(before)
    after_blocks = compute_blocks(after)

    before_b_heading = [b for b in before_blocks if b.kind == "h1"][1]
    after_b_heading = [b for b in after_blocks if b.kind == "h1"][1]
    assert before_b_heading.hash == after_b_heading.hash
    assert before_b_heading.lines == after_b_heading.lines

    before_second_para = [b for b in before_blocks if b.kind == "p"][1]
    after_second_para = [b for b in after_blocks if b.kind == "p"][1]
    assert before_second_para.hash == after_second_para.hash
    assert before_second_para.lines == after_second_para.lines


def test_recompute_is_deterministic():
    assert compute_blocks(_DOC) == compute_blocks(_DOC)


def test_invariant_gate_recompute_from_markdown_matches_the_persisted_map():
    # Section 2.3: a stored block map must always equal a fresh recompute
    # from the same markdown -- the property that makes a divergence a bug,
    # not a modeling choice.
    stored = serialize_blocks(compute_blocks(_DOC))
    recomputed = serialize_blocks(compute_blocks(_DOC))

    assert stored == recomputed


def test_serialize_blocks_omits_sub_and_rows_when_absent():
    plain_paragraph = compute_blocks("intro only\n")
    [entry] = serialize_blocks(plain_paragraph)

    assert entry == {
        "kind": "p",
        "hash": plain_paragraph[0].hash,
        "lines": 1,
        "gap": 0,
    }
    assert "sub" not in entry
    assert "rows" not in entry


def test_block_map_persists_local_separator_gaps_for_exact_prefix_locations():
    blocks = compute_blocks("# A\n\nfirst\n\n\nsecond\n")

    assert [(block.lines, block.gap, block.line_start) for block in blocks] == [
        (1, 0, 0),
        (1, 1, 2),
        (1, 2, 5),
    ]


def test_block_map_uses_raw_coordinates_for_bare_cr_newlines():
    body = "# Title\r\rfirst\r\rsecond"

    blocks = compute_blocks(body)

    assert [block.kind for block in blocks] == ["h1", "p", "p"]
    assert (blocks[0].line_start, blocks[0].line_end) == (0, 1)


def test_block_map_and_outline_classify_a_bom_heading_the_same_way():
    blocks = compute_blocks("\ufeff# Title\n\nbody\n")

    assert [block.kind for block in blocks] == ["h1", "p"]
    assert blocks[0].line_start == 0


def test_frontmatter_closed_with_dots_does_not_swallow_following_body():
    body = "---\ntitle: Example\n...\nBody starts immediately\n"

    blocks = compute_blocks(body)

    assert [block.kind for block in blocks] == ["p"]
    assert blocks[0].line_start == 3

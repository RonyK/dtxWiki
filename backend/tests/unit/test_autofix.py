from backend.services.lint.autofix import (
    MarkdownBlockKind,
    analyze_markdown,
    normalize_markdown,
    safe_normalize_markdown_legacy_v1,
    table_row_semantic_key,
)

# Inputs that exercise every transform + edge cases; all must be idempotent and
# must never corrupt fenced code or frontmatter.
SAMPLES = [
    "# Title\n\nBody.\n",
    "#NotAHeading paragraph keeps its hash.\n",
    "##   Spacey   ##   \n\nx\n",
    "* one\n+ two\n- three\n",
    "para\n\n\n\n\nafter many blanks\n",
    "trailing spaces here    \nand a tab\there\t\n",
    "a  \nb\n\n* first  \n  continuation\n",
    "```python\n   x = 1   \n## not a heading inside code\n* not a list\n```\n",
    "---\ntitle: Keep\nslug: keep-me\n---\n\n# Body\n\ntext\n",
    "| a | bbbb |\n|---|---|\n| 1 | 2 |\n",
    "| name | role |\n| :--- | ---: |\n| ab | x |\n",
    "Prose with a | pipe but no table separator.\nSecond | line of prose.\n",
    "````\n```\ninner\n````\n",  # nested fence preserved
]


def test_normalize_is_idempotent():
    for src in SAMPLES:
        once = normalize_markdown(src)
        assert normalize_markdown(once) == once, src


def test_fenced_code_is_verbatim():
    src = "```python\n   x = 1   \n## not a heading\n* not a list\n   \n```\n"
    out = normalize_markdown(src)
    assert "   x = 1   " in out  # interior whitespace untouched
    assert "## not a heading" in out
    assert "* not a list" in out


def test_four_space_indented_marker_does_not_close_a_commonmark_fence():
    src = "```text\ncode\n    ```\n* still fenced\n"

    analysis = analyze_markdown(src)

    assert analysis.normalized == src
    assert all(block.kind == MarkdownBlockKind.FENCE for block in analysis.blocks)


def test_frontmatter_is_preserved_verbatim():
    src = "---\ntitle: Keep\nslug: keep-me\n---\n\n# Body\n"
    out = normalize_markdown(src)
    assert out.startswith("---\ntitle: Keep\nslug: keep-me\n---\n")


def test_paragraph_starting_with_hash_is_not_made_a_heading():
    assert normalize_markdown("#nospace\n") == "#nospace\n"


def test_heading_spacing_and_atx_close_normalized():
    assert normalize_markdown("##   Spacey   ##  \n") == "## Spacey\n"


def test_unordered_markers_normalized_to_dash():
    assert normalize_markdown("* a\n+ b\n- c\n") == "- a\n- b\n- c\n"


def test_trailing_whitespace_stripped_without_losing_markdown_hard_breaks():
    assert normalize_markdown("text    \n") == "text\n"
    assert normalize_markdown("a  \nb") == "a  \nb\n"
    assert normalize_markdown("a    \nb\t\n") == "a  \nb\n"
    assert normalize_markdown("a\t\nb\n") == "a\nb\n"
    assert normalize_markdown("* first  \n  continuation\n") == (
        "- first  \n  continuation\n"
    )
    assert normalize_markdown("foo | bar  \nbaz | qux\n") == (
        "foo | bar  \nbaz | qux\n"
    )
    assert normalize_markdown("a\\\nb\n") == "a\\\nb\n"
    assert safe_normalize_markdown_legacy_v1("a  \nb\n") == "a\nb\n"


def test_blank_lines_collapsed():
    assert normalize_markdown("a\n\n\n\nb\n") == "a\n\nb\n"


def test_table_is_aligned():
    out = normalize_markdown("| a | bbbb |\n|---|---|\n| 1 | 2 |\n")
    assert out == "| a   | bbbb |\n| --- | ---- |\n| 1   | 2    |\n"


def test_table_alignment_colons_preserved():
    out = normalize_markdown("| name | role |\n| :--- | ---: |\n| ab | x |\n")
    lines = out.strip().split("\n")
    assert lines[1].startswith("| :") and "-:" in lines[1]


def test_pipe_prose_without_separator_is_not_reshaped():
    src = "Prose with a | pipe.\nSecond | line.\n"
    out = normalize_markdown(src)
    assert out == "Prose with a | pipe.\nSecond | line.\n"


def test_empty_body_unchanged():
    assert normalize_markdown("") == ""


# --- Adversarial inputs from the merged review (I1, I2, I4, I5, I6, I12) ---


def test_ragged_table_does_not_crash_and_is_left_unchanged():
    # I1/I12: a data row wider than the separator must not raise; fail closed
    # (the ambiguous block is emitted verbatim, not padded into a new shape).
    src = "| a | b | c |\n| --- | --- |\n| x | y | z |\n"
    out = normalize_markdown(src)  # must not raise
    assert "| x | y | z |" in out
    assert "---" in out  # the original 2-cell separator is untouched
    assert normalize_markdown(out) == out


def test_separator_wider_than_data_does_not_crash():
    src = "| a | b |\n| --- | --- | --- |\n| x | y |\n"
    out = normalize_markdown(src)
    assert normalize_markdown(out) == out


def test_escaped_pipe_kept_in_single_cell():
    # I1: a backslash-escaped pipe is not a column delimiter.
    src = "| a | b |\n| --- | --- |\n| x \\| y | z |\n"
    out = normalize_markdown(src)
    assert "x \\| y" in out
    assert normalize_markdown(out) == out


def test_code_span_pipe_kept_in_single_cell():
    # I1: a pipe inside an inline code span is not a column delimiter.
    src = "| a | b |\n| --- | --- |\n| `x|y` | z |\n"
    out = normalize_markdown(src)
    assert "`x|y`" in out
    assert normalize_markdown(out) == out


def test_fenced_code_blank_lines_preserved():
    # I2: blank lines inside a fence survive (PEP8 two blanks between functions).
    src = "```python\ndef a():\n    pass\n\n\ndef b():\n    pass\n```\n"
    out = normalize_markdown(src)
    assert "pass\n\n\ndef b" in out
    assert normalize_markdown(out) == out


def test_indented_code_block_not_rewritten():
    # I4: a 4-space indented code block is verbatim (markers + trailing ws).
    src = "para\n\n    * not a bullet\n    keep   \n"
    out = normalize_markdown(src)
    assert "    * not a bullet" in out
    assert "    keep   " in out  # trailing whitespace inside code preserved
    assert normalize_markdown(out) == out


def test_spaced_thematic_break_not_made_a_list():
    # I5: a spaced "* * *" is a CommonMark thematic break (markers -, *, _), not
    # a list item, so it must NOT be rewritten to "- * *".
    assert normalize_markdown("* * *\n") == "* * *\n"
    assert normalize_markdown("***\n") == "***\n"
    assert normalize_markdown("- - -\n") == "- - -\n"
    assert normalize_markdown("___\n") == "___\n"
    # "+" is NOT a thematic-break marker in CommonMark, so "+ + +" is a genuine
    # list item and normalizing the marker to "-" is correct + meaning-preserving.
    assert normalize_markdown("+ + +\n") == "- + +\n"


def test_heading_content_hash_preserved():
    # I6: a trailing content "#" (C#, F#) is not an ATX closing sequence.
    assert normalize_markdown("# C#\n") == "# C#\n"
    assert normalize_markdown("## F#\n") == "## F#\n"
    # a real ATX closer (space-preceded) is still stripped
    assert normalize_markdown("# Title ###\n") == "# Title\n"


def test_pipe_prose_then_separator_line_not_a_table():
    # I12: prose containing a pipe followed by a separator-looking line is not a
    # GFM table (separator not immediately after a header row) -> left as prose.
    src = "Use the foo | bar syntax.\n--- | ---\nMore prose here.\n"
    out = normalize_markdown(src)
    assert "Use the foo | bar syntax." in out
    assert "More prose here." in out
    assert normalize_markdown(out) == out


def test_frontmatter_body_adjacency_preserved():
    # I14: do not insert a blank between frontmatter and body if source had none.
    assert (
        normalize_markdown("---\ntitle: T\n---\n# Body\n")
        == "---\ntitle: T\n---\n# Body\n"
    )


def test_analysis_keeps_normalized_bytes_and_source_ranges_in_one_pass():
    src = (
        "---\r\n"
        "title: T  \r\n"
        "---\r\n"
        "\r\n"
        "##   H ###  \r\n"
        "\r\n"
        "* item  \r\n"
        "\r\n"
        "| a | bbbb |\r\n"
        "|---|---|\r\n"
        "| 1 | 2 |\r\n"
    )
    expected = (
        "---\n"
        "title: T  \n"
        "---\n"
        "\n"
        "## H\n"
        "\n"
        "- item\n"
        "\n"
        "| a   | bbbb |\n"
        "| --- | ---- |\n"
        "| 1   | 2    |\n"
    )

    analysis = analyze_markdown(src)

    assert analysis.normalized == expected
    assert normalize_markdown(src) == expected
    assert analysis.canonical is False
    assert [block.kind for block in analysis.blocks] == [
        MarkdownBlockKind.FRONTMATTER,
        MarkdownBlockKind.BLANK,
        MarkdownBlockKind.HEADING,
        MarkdownBlockKind.BLANK,
        MarkdownBlockKind.LIST,
        MarkdownBlockKind.BLANK,
        MarkdownBlockKind.TABLE,
    ]
    assert analysis.source_line_starts.typecode == "I"
    assert analysis.source_line_to_output_line.typecode == "i"
    assert analysis.source_line_to_block.typecode == "i"

    heading_point = src.index("##")
    heading = analysis.block_at_source_point(heading_point)
    assert heading is not None
    assert heading.kind == MarkdownBlockKind.HEADING
    assert src[heading.source_start : heading.source_end] == "##   H ###  \r\n"
    heading_line_index = analysis.source_line_index_at(heading_point)
    assert heading_line_index is not None
    assert analysis.source_line_span(heading_line_index) == (
        heading.source_start,
        heading.source_end - 2,
        heading.source_end,
    )

    table_point = src.index("| a")
    table = analysis.block_at_source_point(table_point)
    assert table is not None
    assert table.kind == MarkdownBlockKind.TABLE
    assert table.well_formed is True
    assert src[table.source_start : table.source_end].startswith("| a | bbbb |\r\n")
    assert analysis.normalized[table.normalized_start : table.normalized_end] == (
        "| a   | bbbb |\n| --- | ---- |\n| 1   | 2    |\n"
    )
    table_lines = analysis.block_lines(table)
    assert table_lines[2].table_row_key == table_row_semantic_key("| 1 | 2 |")
    table_line = analysis.line_at_source_point(table_point)
    assert table_line is not None
    assert table_line.line_index == table.first_line
    assert table_line.table_row_key == table_lines[0].table_row_key
    assert analysis.block_at_source_point(len(src)) is None


def test_source_line_lookup_preserves_mixed_terminators_and_dropped_blanks():
    src = "A\r\n\n\nB\rC"

    analysis = analyze_markdown(src)

    assert analysis.normalized == "A\n\nB\nC\n"
    assert list(analysis.source_line_starts) == [0, 3, 4, 5, 7]
    assert analysis.source_line_span(0) == (0, 1, 3)
    assert analysis.source_line_span(3) == (5, 6, 7)
    assert analysis.source_line_span(-1) == (7, 8, 8)
    assert analysis.line_at_source_point(3) is not None
    assert analysis.line_at_source_point(4) is None
    assert analysis.block_at_source_point(4) is None


def test_analysis_classifies_protected_blocks_without_reparsing_them():
    src = "```text\n* untouched  \n```\n\n    + indented  \n\nBody\n"

    analysis = analyze_markdown(src)

    assert analysis.canonical is True
    assert [block.kind for block in analysis.blocks] == [
        MarkdownBlockKind.FENCE,
        MarkdownBlockKind.BLANK,
        MarkdownBlockKind.INDENTED_CODE,
        MarkdownBlockKind.BLANK,
        MarkdownBlockKind.BODY,
    ]
    for block in analysis.blocks:
        assert analysis.block_at_source_point(block.source_start) == block


def test_table_row_semantic_key_ignores_layout_but_honors_pipe_syntax():
    compact = table_row_semantic_key(r"| x \| y | `a|b` | --- |")
    padded = table_row_semantic_key(r"x \| y     | `a|b` | ---")

    assert compact == padded
    assert compact == (
        ("text", r"x \| y"),
        ("text", "`a|b`"),
        ("text", "---"),
    )
    assert table_row_semantic_key(
        "| :--- | ---: |", separator=True
    ) == table_row_semantic_key(":------------ | ------------:", separator=True)
    assert table_row_semantic_key("| --- |") != table_row_semantic_key("| ----- |")


def test_analysis_exposes_table_matrix_with_escaped_and_inline_pipes():
    src = "| first | second |\n| --- | --- |\n| x \\| y | `a|b` |\n"

    analysis = analyze_markdown(src)
    table = analysis.blocks[0]

    assert table.kind == MarkdownBlockKind.TABLE
    assert analysis.line_at_output_index(table.first_line + 2).table_row_key == (
        ("text", r"x \| y"),
        ("text", "`a|b`"),
    )


def test_ragged_pipe_run_is_not_promoted_to_table_semantics():
    src = "| a | b |\n| --- | --- |\n| x | y | z |\n"

    analysis = analyze_markdown(src)
    block = analysis.blocks[0]

    assert analysis.normalized == src
    assert block.kind == MarkdownBlockKind.PIPE_RUN
    assert block.well_formed is False
    assert analysis.line_at_output_index(block.first_line + 2).table_row_key is None

import random

import pytest
from markdown_it import MarkdownIt

from backend.utils.markdown_outline import parse_outline
from backend.utils.markdown_parser import parse_markdown

pytestmark = pytest.mark.unit


def test_outline_heading_projection_matches_commonmark_property_sweep():
    """Section 8 gate: outline headings are exactly CommonMark level-0 headings.

    The fixed seed makes the generated grammar reproducible while crossing the
    contexts that historically caused parser disagreement: nested containers,
    raw HTML, both fence markers, tables, frontmatter, Setext headings, BOMs,
    and all three newline spellings.
    """

    rng = random.Random(260829)
    fragments = (
        "# Alpha\nbody\n",
        "Beta\n====\nbody\n",
        "> # quoted, not document-level\n> body\n",
        "- ## listed, not document-level\n  body\n",
        "```md\n# fenced\n```\n",
        "~~~~\n## fenced too\n~~~~\n",
        "<div>\n# html content\n</div>\n",
        "| A | B |\n| - | - |\n| # cell | value |\n",
        "    # indented code\n",
        "---\nthematic break context\n",
        "### Gamma ###\n",
        "plain paragraph\n\n",
    )
    for case_index in range(512):
        newline = rng.choice(("\n", "\r\n", "\r"))
        selected = [rng.choice(fragments) for _ in range(rng.randint(1, 10))]
        prefix = ""
        if rng.randrange(4) == 0:
            prefix = "---\ntitle: '# metadata'\n---\n"
        if rng.randrange(5) == 0:
            prefix = "\ufeff" + prefix
        body = (prefix + "".join(selected)).replace("\n", newline)

        parsed = parse_markdown(body)
        tokens = parsed.tokens
        expected: list[tuple[int, int, str]] = []
        for token_index, token in enumerate(tokens):
            if token.type != "heading_open" or token.level != 0 or token.map is None:
                continue
            inline = tokens[token_index + 1]
            expected.append((token.map[0] + 1, int(token.tag[1:]), inline.content))
        actual = [
            (section.line_start, section.level, section.title)
            for section in parse_outline(body).sections
            if section.kind == "heading"
        ]

        assert actual == expected, f"generated CommonMark case {case_index} diverged"


def test_outline_tracks_hierarchy_duplicates_and_normalized_ids():
    body = (
        "# Caf\u00e9\nOne.\n"
        "## Setup / Start\nFirst.\n"
        "## Setup / Start\nSecond.\n"
        "# Cafe\u0301\nTwo.\n"
        "# Caf\u00e9\nThree.\n"
    )

    headings = [
        section for section in parse_outline(body).sections if section.kind == "heading"
    ]

    assert [section.heading_path for section in headings] == [
        ("Caf\u00e9",),
        ("Caf\u00e9", "Setup / Start"),
        ("Caf\u00e9", "Setup / Start"),
        ("Cafe\u0301",),
        ("Caf\u00e9",),
    ]
    assert [section.occurrence for section in headings] == [1, 1, 2, 1, 2]
    assert [section.section_id for section in headings] == [
        "caf\u00e9",
        "setup-start",
        "setup-start-2",
        "caf\u00e9-2",
        "caf\u00e9-3",
    ]


def test_outline_ignores_headings_inside_marker_specific_fences():
    body = (
        "# Real\n"
        "~~~python\n# fake tilde\n```\n## still fake\n~~~\n"
        "## Child\n"
        "```text\n# fake backtick\n~~~\n### still fake\n```\n"
        "### Grandchild\n"
    )

    headings = [
        section for section in parse_outline(body).sections if section.kind == "heading"
    ]

    assert [section.title for section in headings] == [
        "Real",
        "Child",
        "Grandchild",
    ]
    assert headings[-1].heading_path == ("Real", "Child", "Grandchild")


@pytest.mark.parametrize(
    "body",
    [
        "# Real\n````python\ncode\n```\n# still fenced\n",
        "# Real\n```python\ncode\n```not-a-close\n# still fenced\n",
        "# Real\n~~~python\ncode\n~~~ trailing\n# still fenced\n",
    ],
)
def test_outline_requires_a_marker_only_fence_at_least_as_long_as_the_opener(body):
    headings = [
        section for section in parse_outline(body).sections if section.kind == "heading"
    ]

    assert [section.title for section in headings] == ["Real"]


def test_outline_recognizes_a_sufficient_marker_only_fence_closer():
    body = "# Real\n````python\ncode\n`````   \n# Visible\n"

    headings = [
        section for section in parse_outline(body).sections if section.kind == "heading"
    ]

    assert [section.title for section in headings] == ["Real", "Visible"]


def test_outline_does_not_accept_a_four_space_indented_fence_closer():
    body = "# Real\n```text\ncode\n    ```\n# Still fenced\n"

    headings = [
        section for section in parse_outline(body).sections if section.kind == "heading"
    ]

    assert [section.title for section in headings] == ["Real"]


def test_outline_accepts_zero_to_three_space_atx_headings_only():
    body = " # One\n   ## Two\n    ### code, not a heading\n"

    headings = [
        section for section in parse_outline(body).sections if section.kind == "heading"
    ]

    assert [section.title for section in headings] == ["One", "Two"]


def test_outline_always_parses_via_commonmark_regardless_of_html_markers(monkeypatch):
    """Every outline parse uses markdown-it, including bodies without raw HTML.

    A separate regex parser could disagree with the CommonMark structure.
    """
    calls = []
    real_parse = MarkdownIt.parse

    def counting_parse(self, *args, **kwargs):
        calls.append(args)
        return real_parse(self, *args, **kwargs)

    monkeypatch.setattr(MarkdownIt, "parse", counting_parse)
    body = "# Real\n```text\n# fenced, not a heading\n```\n## Child\n"

    headings = [
        section for section in parse_outline(body).sections if section.kind == "heading"
    ]

    assert [section.title for section in headings] == ["Real", "Child"]
    assert len(calls) == 1


def test_flat_and_subtree_ends_are_distinct_and_use_raw_offsets():
    body = (
        "Preamble\n"
        "# Parent\nparent body\n"
        "## Child\nchild body\n"
        "### Grand\ngrand body\n"
        "# Next\nnext body\n"
    )
    outline = parse_outline(body)
    parent = next(section for section in outline.sections if section.title == "Parent")
    child = next(section for section in outline.sections if section.title == "Child")
    grand = next(section for section in outline.sections if section.title == "Grand")
    next_section = next(
        section for section in outline.sections if section.title == "Next"
    )

    assert body[parent.char_start : parent.char_end] == "# Parent\nparent body\n"
    assert body[parent.char_start : parent.subtree_char_end] == (
        "# Parent\nparent body\n## Child\nchild body\n### Grand\ngrand body\n"
    )
    assert child.char_end == grand.char_start
    assert child.subtree_char_end == next_section.char_start
    assert grand.char_end == grand.subtree_char_end == next_section.char_start
    assert next_section.line_end == 9


def test_frontmatter_is_separate_from_preamble_and_not_scanned_for_headings():
    body = "---\n# metadata comment\ntags: [beta]\n---\nIntro.\n# Real\nBody."

    sections = parse_outline(body).sections

    assert [section.kind for section in sections] == [
        "frontmatter",
        "preamble",
        "heading",
    ]
    frontmatter, preamble, heading = sections
    assert body[frontmatter.char_start : frontmatter.char_end] == (
        "---\n# metadata comment\ntags: [beta]\n---\n"
    )
    assert frontmatter.level == 0
    assert preamble.level == 0
    assert preamble.heading_path == ()
    assert (frontmatter.occurrence, preamble.occurrence) == (1, 2)
    assert body[preamble.char_start : preamble.char_end] == "Intro.\n"
    assert heading.title == "Real"
    assert heading.heading_path == ("Real",)


def test_crlf_coordinates_and_heading_body_start_reference_raw_markdown():
    body = "# Parent\r\nbody\r\n## Child\r\ntail"

    headings = [
        section for section in parse_outline(body).sections if section.kind == "heading"
    ]
    parent, child = headings

    assert parent.line_start == 1
    assert parent.line_end == 2
    assert parent.char_start == 0
    assert parent.body_char_start == len("# Parent\r\n")
    assert parent.char_end == child.char_start == len("# Parent\r\nbody\r\n")
    assert body[child.body_char_start :] == "tail"
    assert child.line_start == 3
    assert child.line_end == 4


def test_bare_cr_coordinates_reference_the_untouched_markdown():
    body = "# Parent\rbody\r## Child\rtail"

    headings = [
        section for section in parse_outline(body).sections if section.kind == "heading"
    ]
    parent, child = headings

    assert parent.char_start == 0
    assert parent.body_char_start == len("# Parent\r")
    assert parent.char_end == child.char_start == len("# Parent\rbody\r")
    assert body[child.body_char_start :] == "tail"
    assert (parent.line_end, child.line_end) == (2, 4)


def test_outline_ignores_a_hash_line_inside_a_raw_html_block():
    # dtxWiki review, 260827: a "#" line inside a raw HTML block (e.g. a
    # <div> admonition -- ordinary content in a wiki that allows raw HTML)
    # was misread as a real heading, splitting the enclosing section early
    # and leaving the rest of the HTML block orphaned outside any section.
    body = (
        "# Real Title\n\n"
        "Intro text.\n\n"
        '<div class="callout">\n'
        "# not a heading, just html content\n"
        "It explains something.\n"
        "</div>\n\n"
        "## Real Section Two\n\n"
        "more text\n"
    )

    outline = parse_outline(body)
    headings = [section for section in outline.sections if section.kind == "heading"]

    assert [section.title for section in headings] == ["Real Title", "Real Section Two"]
    real_title, real_section_two = headings
    # "Real Section Two" is level 2 -- a CHILD of level-1 "Real Title", not a
    # sibling that closes its subtree -- so the HTML block (including its
    # literal "#" line) is inside "Real Title"'s flat span, not cut off into
    # an orphaned section of its own the way it was pre-fix.
    assert "not a heading" in body[real_title.char_start : real_title.char_end]
    assert real_title.char_end == real_section_two.char_start
    # And the child genuinely nests: the parent's subtree reaches past it.
    assert real_title.subtree_char_end == len(body)


def test_outline_recognizes_a_heading_behind_a_leading_bom():
    # dtxWiki review, 260827: a leading byte-order mark (common from
    # Windows-authored files) made the first line's "#" invisible to the
    # heading regex, silently dropping the document's only heading from the
    # outline even though it renders normally to any human or BOM-aware tool.
    body = "\ufeff# Heading One\n\ncontent\n"

    outline = parse_outline(body)
    headings = [section for section in outline.sections if section.kind == "heading"]

    assert [section.title for section in headings] == ["Heading One"]
    heading = headings[0]
    # Raw coordinates are untouched -- the BOM is still the first character
    # of the section's own slice, not silently dropped from the body.
    assert body[heading.char_start : heading.char_end].startswith("\ufeff# Heading One")
    assert heading.char_start == 0


def test_outline_strips_the_optional_atx_closing_sequence_from_the_title():
    # dtxWiki review, 260827: the closing "##" in "## Section Title ##" used
    # to stay glued onto `title`/`heading_path`, so a caller addressing the
    # heading by its human-visible text (what any renderer shows) got
    # section_not_found. Per CommonMark, the closing run must be preceded by
    # a space/tab -- "## C#" is NOT a closing sequence and must survive.
    body = "## Section Title ##\n\ntext\n\n## C#\n\ntext\n"

    outline = parse_outline(body)
    headings = [section for section in outline.sections if section.kind == "heading"]

    assert [section.title for section in headings] == ["Section Title", "C#"]
    assert [section.heading_path for section in headings] == [
        ("Section Title",),
        ("C#",),
    ]

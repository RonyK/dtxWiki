"""replace_text must not treat Markdown structure as a copy slip.

Regression cases from an adversarial sweep of the tolerant matcher. Each
REFUSED case once applied, silently changing structure the caller did not
target, while the receipt called the difference invisible or a slip. The
APPLIED cases are controls: ordinary prose inside the same constructs must
stay forgivable.
"""

import pytest
from pydantic import TypeAdapter

from backend.schemas.context_edit import EditOperation
from backend.services.context_editing import ContextEditError, apply_operations
from backend.services.edit_receipt import build_edit_receipt, patch_operation_receipts
from backend.utils.block_identity import compute_block_map_with_ids
from backend.utils.markdown_outline import parse_outline

pytestmark = pytest.mark.unit

_OPERATION = TypeAdapter(EditOperation)
NBSP = chr(0xA0)


def _run(body: str, op: dict):
    block_map = compute_block_map_with_ids(
        body, previous_body_md=None, previous_block_map=None, identity_seed="t"
    )
    try:
        result, edits = apply_operations(
            body,
            parse_outline(body),
            [_OPERATION.validate_python(op)],
            block_map=block_map,
        )
    except ContextEditError as exc:
        return "REFUSED", exc.error_code, None
    return "APPLIED", edits[0].match_kind, result


def _text(old: str, new: str = "x", **extra) -> dict:
    return {"op": "replace_text", "old_text": old, "new_text": new, **extra}


def _section(*path: str) -> dict:
    return {"by": "heading_path", "heading_path": list(path)}


REFUSED = {
    # A: a line's prefix (indentation, block markers) is structure.
    "one-space indent re-nests a list item": (
        "# H\n\n- alpha item\n - beta item\n\nend\n",
        _text("- alpha item\n  - beta item", "- alpha item\n  - beta ITEM"),
    ),
    "marker spacing moves the content column": (
        "# H\n\n-   run this\n\n      rm -rf build\n",
        _text("- run this", "- run THIS"),
    ),
    "a no-break space after # is not a heading": (
        "intro\n\n#" + NBSP + "Title here\n\nmore\n",
        _text("# Title here", "# Title HERE"),
    ),
    "a no-break space after - is not a list": (
        "# H\n\n-" + NBSP + "not a list item\n",
        _text("- not a list item", "- not a list ITEM"),
    ),
    "a fuzzy slip in indentation": (
        "# H\n\n- alpha item goes here\n  - beta item goes there\n\nend\n",
        _text(
            "- alpha item goes here\n - beta item goes there",
            "x",
            at=_section("H"),
        ),
    ),
    "old_text ending in spaces adds a hard break": (
        "Address line one\nAddress line two\n",
        _text("Address line one  \n", "Address line 1  \n"),
    ),
    "old_text ending in a tab eats a hard break": (
        "Address line one  \nAddress line two\n",
        _text("Address line one\t", "Address line 1"),
    ),
    "a hard break at the edge of an anchor scope": (
        "Intro.\n\nAddress line one  \nAddress line two\n",
        _text(
            "Address line one\n",
            "Address line 1\n",
            at={
                "by": "anchor",
                "anchor_text": "Address line one  \n",
                "context_before": "",
                "context_after": "",
                "char_offset_hint": 8,
            },
        ),
    ),
    # B: exact-only zones are exact on BOTH sides.
    "curly quotes typed into a code block": (
        '# Top\n\n```python\nprint("hello")\n```\n',
        _text("print(“hello”)", "print(“hi”)"),
    ),
    "a dash typed into a URL": (
        "# Top\n\nRead https://ex.com/setup-guide today.\n",
        _text("https://ex.com/setup–guide", "https://ex.com/other"),
    ),
    "curly quotes typed into frontmatter": (
        '---\ntitle: "Release log"\n---\n# Top\n\nBody.\n',
        _text("title: “Release log”", "title: “Bye”"),
    ),
    "a stray backtick must not unprotect a later code span": (
        '# Top\n\nA lone ` tick here.\n\nThen run `echo "bye"` now.\n',
        _text("run `echo “bye”` now", 'run `echo "hi"` now'),
    ),
    "whitespace inside raw-HTML code": (
        "# S\n\nRun <code>ls  -l /tmp</code> to list files in the temp directory.\n",
        _text("Run <code>ls -l /tmp</code> to list", "Run it"),
    ),
    "a slip inside raw-HTML code, nested in a list": (
        "# S\n\n- Step one\n  - On the server run "
        "<code>sudo systemctl restart nginx now --force</code> and wait.\n",
        _text(
            "On the server run <code>sudo systemctl restarr nginx now --force</code>"
            " and wait.",
            "x",
            at=_section("S"),
        ),
    ),
    "a slip in a wikilink target": (
        "# Top\n\nFollow the [[Deployment Guide]] before the next release window.\n",
        _text(
            "Follow the [[Deployment Guids]] before the next release window.",
            "x",
            at=_section("Top"),
        ),
    ),
    "curly quotes in a link title": (
        'Intro.\n\nSee [the manual](./setup-guide.md "Setup") first.\n',
        _text("[the manual](./setup-guide.md “Setup”)", "[the guide](./x.md)"),
    ),
    "a curly apostrophe typed into math": (
        "# Top\n\nThe derivative $f'(x) = 3x$ is linear.\n",
        _text("$f’(x) = 3x$", "$g(x)$"),
    ),
    "a slip in a file path": (
        "# Top\n\nThe configuration lives in /etc/nginx/sites-enabled/default today.\n",
        _text(
            "The configuration lives in /etc/ngimx/sites-enabled/default today.",
            "x",
            at=_section("Top"),
        ),
    ),
    "a slip in a prerelease version": (
        "# Top\n\nUpgrade to release v2.4.1-beta before Monday please.\n",
        _text(
            "Upgrade to release v2.4.1-beda before Monday please.",
            "x",
            at=_section("Top"),
        ),
    ),
    "dropping a link's opening bracket": (
        "# Top\n\nPlease read the [setup guide](./install/overview.md)"
        " before we start.\n",
        _text(
            "Please read the setup guide](./install/overview.md) before we start.",
            "x",
            at=_section("Top"),
        ),
    ),
    "a CRLF table's delimiter row is not a slip": (
        "# T\r\n\r\n| Name | Value |\r\n|:---|---:|\r\n| a | b |\r\n",
        _text(
            "| Name | Value |\n|----|---:|",
            "| Name | Amount |\n|----|---:|",
            at=_section("T"),
        ),
    ),
    # E: equally close repetitions are rivals, not a first-wins.
    "short-period repetition": (
        "# Poll\n\nAnswers: " + ", ".join(["yes"] * 21) + ".\n",
        _text(
            ", ".join(["yes"] * 4) + ", yez, " + ", ".join(["yes"] * 5),
            "REPLACED",
            at=_section("Poll"),
        ),
    ),
}

APPLIED = {
    "a slip in list-item prose": (
        "# H\n\n- Install the package and restart the service afterwards.\n",
        _text(
            "- Install the package and restat the service afterwards.",
            "- Install the package, then restart the service.",
            at=_section("H"),
        ),
        "# H\n\n- Install the package, then restart the service.\n",
    ),
    "a slip in link text, destination copied exactly": (
        "# Top\n\nPlease read the [setup guide for new users](./x.md)"
        " before we start.\n",
        _text(
            "Please read the [setup guide for new usrs](./x.md) before we start.",
            "Read the [guide](./x.md) first.",
            at=_section("Top"),
        ),
        "# Top\n\nRead the [guide](./x.md) first.\n",
    ),
    "double spaces between words, mid-line": (
        "# Top\n\nThe  quick  brown fox.\n",
        _text("The quick brown fox.", "A fox."),
        "# Top\n\nA fox.\n",
    ),
    "a hard break copied with its spaces": (
        "Address line one  \nAddress line two\n",
        _text(
            "Address line one  \nAddress line two", "Address line 1  \nAddress line two"
        ),
        "Address line 1  \nAddress line two\n",
    ),
    "code copied exactly": (
        '# Top\n\n```python\nprint("hello")\n```\n',
        _text('print("hello")', 'print("hi")'),
        '# Top\n\n```python\nprint("hi")\n```\n',
    ),
}


@pytest.mark.parametrize("name", sorted(REFUSED))
def test_structure_is_never_matched_loosely(name):
    body, op = REFUSED[name]
    outcome = _run(body, op)
    assert outcome[0] == "REFUSED", (name, outcome)


@pytest.mark.parametrize("name", sorted(APPLIED))
def test_ordinary_prose_stays_forgivable(name):
    body, op, expected = APPLIED[name]
    outcome = _run(body, op)
    assert outcome[0] == "APPLIED", (name, outcome)
    assert outcome[2] == expected, name


def test_a_cr_only_page_keeps_cr_after_a_normalized_match():
    body = "Para one\rstill para\r\rNext\r"
    outcome = _run(body, _text("Para one\nstill para", "Para 1\nstill para"))
    assert outcome == ("APPLIED", "normalized", "Para 1\rstill para\r\rNext\r")


def test_an_lf_page_stays_lf_when_the_caller_sends_crlf():
    body = "# T\n\nalpha line\nbeta line\n\nomega\n"
    outcome = _run(body, _text("alpha line\r\nbeta line", "ALPHA\r\nBETA"))
    assert outcome == ("APPLIED", "normalized", "# T\n\nALPHA\nBETA\n\nomega\n")


def test_each_span_keeps_its_own_line_endings():
    body = "A  B\r\nC\r\n\r\nA  B\nC\n"
    outcome = _run(body, _text("A B\nC", "Z\nW", expected_occurrences=2))
    assert outcome == ("APPLIED", "normalized", "Z\r\nW\r\n\r\nZ\nW\n")


def test_line_numbers_are_right_on_a_cr_only_page():
    body = "# T\r\rfirst para\r\rThe quick brown fox jumps over the lazy dog.\r"
    block_map = compute_block_map_with_ids(
        body, previous_body_md=None, previous_block_map=None, identity_seed="t"
    )
    with pytest.raises(ContextEditError) as refused:
        apply_operations(
            body,
            parse_outline(body),
            [
                _OPERATION.validate_python(
                    _text("The quick brown fox jumpd over the lazy dog.")
                )
            ],
            block_map=block_map,
        )
    assert refused.value.details[0].candidates[0].line_start == 5

    operations = [_OPERATION.validate_python(_text("first para", "second"))]
    _result, edits = apply_operations(
        body, parse_outline(body), operations, block_map=block_map
    )
    receipt = patch_operation_receipts(
        body=body, operations=operations, edits=edits, block_map=block_map
    )
    assert receipt[0].line_range == (3, 3)


def test_an_overlap_on_a_cr_only_page_names_lines_and_the_operation():
    body = "# T\r\rfirst para\r\rone two three four\r"
    block_map = compute_block_map_with_ids(
        body, previous_body_md=None, previous_block_map=None, identity_seed="t"
    )
    with pytest.raises(ContextEditError) as refused:
        apply_operations(
            body,
            parse_outline(body),
            [
                _OPERATION.validate_python(_text("one two", "1 2")),
                _OPERATION.validate_python(_text("two three", "2 3")),
            ],
            block_map=block_map,
        )
    assert refused.value.error_code == "operations_overlap"
    detail = refused.value.details[0]
    assert detail.operation_index == 1
    assert "(replace_text, line 5)" in detail.message


def test_a_receipt_counts_lines_on_a_cr_only_page_as_on_an_lf_one():
    def counts(newline: str) -> tuple[int, int]:
        receipt = build_edit_receipt(
            edit_mode="whole_body",
            before_body=newline.join(["# T", "", "first", "gone", "", "last", ""]),
            after_body=newline.join(["# T", "", "first", "two", "three", "", ""]),
            editing_revision_before=1,
            editing_revision_after=2,
        )
        return receipt.lines_added, receipt.lines_removed

    assert counts("\n") == (2, 2)
    assert counts("\r") == counts("\n")
    assert counts("\r\n") == counts("\n")
